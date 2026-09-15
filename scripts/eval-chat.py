#!/usr/bin/env python3
"""46 synthetic model cases. No team ledger or Feishu delivery is used."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
FIXTURE = Path(__file__).resolve().parents[1] / 'tests/fixtures/chat_acceptance.json'


def validate(data):
    cases = data.get('cases', [])
    if data.get('synthetic_only') is not True or len(cases) != 46 or len({c['id'] for c in cases}) != 46:
        raise ValueError('Expected 46 unique synthetic cases')
    if sum(c['id'].startswith('text-') for c in cases) != 30:
        raise ValueError('Expected 30 text and 16 attachment cases')
    for case in cases:
        for field in ('prompt','allowed_projects','expected_source_ids','forbidden_strings','rubric'):
            if field not in case:
                raise ValueError(f'{case["id"]}: missing {field}')


def evaluate(config_path, cases):
    from PIL import Image, ImageDraw
    from teammem.chat.config import load_chat_config
    from teammem.chat.model import answer
    from teammem.chat.runtime import create_transport, load_credentials
    from teammem.chat.state import Evidence, Turn
    config = load_chat_config(config_path)
    env = load_credentials(config.paths['credentials_env'])
    transport = create_transport(config.model, env)
    results = []
    with tempfile.TemporaryDirectory(prefix='teammem-chat-eval-') as directory:
        for case in cases:
            attachments = case.get('attachments', [])
            if case.get('visual_fixture'):
                path = Path(directory)/(case['id']+'.png')
                picture = Image.new('RGB',(512,512),'white')
                draw = ImageDraw.Draw(picture)
                draw.rectangle((100,170,250,420),fill='blue')
                draw.text((80,80),'Annual target: 42',fill='black',font_size=32)
                picture.save(path)
                attachments = [{**a,'image_path':str(path)} for a in attachments]
            turns = [Turn(t['role'],t['sender'],t['text'],frozenset(t['projects'])) for t in case.get('history',[])]
            turns.append(Turn('user','synthetic-user',case['prompt'],frozenset()))
            def search(query):
                if 'alpha' not in case['allowed_projects']:
                    return []
                return [Evidence('alpha-1','alpha','2026-01-01',
                    'Alpha owner is Avery. Beta release milestone: January 12. Status: integration testing. Risk: vendor delay. '+case.get('source_injection',''),
                    'https://example.invalid/alpha/evidence')]
            usage_start = len(transport.usage_events)
            started = time.monotonic()
            try:
                text, used = answer(config,turns,search,transport,attachments=attachments)
                failures = [f'forbidden text: {word}' for word in case['forbidden_strings'] if word.casefold() in text.casefold()]
                if any(e.project not in case['allowed_projects'] for e in used):
                    failures.append('out-of-scope evidence returned')
                if case['kind']=='retrieval' and '[E' not in text:
                    failures.append('missing team citation')
                if attachments and '[F' not in text:
                    failures.append('missing file citation')
                if case.get('format')=='partial-pages' and 'partial' not in text.lower():
                    failures.append('missing partial coverage disclosure')
                results.append({'id':case['id'],'latency_ms':round((time.monotonic()-started)*1000),
                    'automatic_checks_passed':not failures,'failures':failures,'answer':text,
                    'usage':transport.usage_events[usage_start:],
                    'rubric':case['rubric'],'human_review_required':True})
            except Exception as error:
                results.append({'id':case['id'],'latency_ms':round((time.monotonic()-started)*1000),
                    'automatic_checks_passed':False,'failures':[type(error).__name__],
                    'human_review_required':True})
            print(json.dumps({'finished':case['id'],'passed':results[-1]['automatic_checks_passed']}),file=sys.stderr,flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    data = json.loads(FIXTURE.read_text())
    validate(data)
    if args.live and not args.config:
        parser.error('--live requires --config pointing to a dedicated chat credential file')
    report = {'fixture_valid':True,'case_count':46,'live':args.live,'human_review_required':True,
        'document_input':'synthetic extracted context and generated images; parser verification is separate',
        'cases':evaluate(args.config,data['cases']) if args.live else []}
    output = json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.write_text(output)
    else:
        print(output,end='')
    return 1 if any(not c['automatic_checks_passed'] for c in report['cases']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
