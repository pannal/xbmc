#!/usr/bin/env python3
"""Check PCM layout settings, localization and optional baseline preservation.

This parses source data; it does not claim GUI rendering or saved-file reload.
"""

import argparse
import ast
import copy
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

XML = 'system/settings/settings.xml'
PO = 'addons/resource.language.{}/resources/strings.po'
NEW = {11: '69363', 12: '69364'}


def po_entries(text):
    entries = {}
    entry = {}
    field = None
    for line in text.splitlines() + ['']:
        if not line.strip():
            if 'msgctxt' in entry:
                context = entry['msgctxt']
                assert context not in entries, f'duplicate PO context {context}'
                entries[context] = entry
            entry, field = {}, None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            field, value = line.split(' ', 1)
            entry[field] = ast.literal_eval(value)
        elif line.startswith('"') and field:
            entry[field] += ast.literal_eval(line)
    return entries


def check(xml, languages, baseline=None):
    tree = ET.fromstring(xml)
    ids = [s.attrib['id'] for s in tree.iter('setting')]
    assert len(ids) == len(set(ids)), 'duplicate setting IDs'
    setting = tree.find('.//setting[@id="audiooutput.channels"]')
    assert setting is not None
    assert setting.findtext('default') == '1'
    assert setting.findtext('level') == '0'
    assert setting.attrib['help'] == '36362'
    dependency = setting.find('dependencies/dependency')
    assert dependency.attrib == {'type': 'visible', 'on': 'property',
                                 'name': 'aesettingvisible', 'setting': 'audiooutput.audiodevice'}
    assert dependency.text == 'audiooutput.channels'
    options = setting.find('constraints/options')
    assert [int(o.text) for o in options] == [1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 9, 10]
    assert {int(o.text): o.attrib['label'] for o in options} == {
        **{n: str(34100 + n) for n in range(1, 11)}, **NEW}
    assert setting.find('control').attrib == {'type': 'list', 'format': 'string'}
    entries = {lang: po_entries(text) for lang, text in languages.items()}
    for value, label in NEW.items():
        name = '6.0' if value == 11 else '6.1'
        assert entries['en_gb']['#' + label]['msgid'] == name
        assert entries['de_de']['#' + label]['msgid'] == name
        assert entries['de_de']['#' + label]['msgstr'] == name
    if baseline:
        old_tree = ET.fromstring(baseline[XML])
        preserved = copy.deepcopy(tree)
        preserved_options = preserved.find('.//setting[@id="audiooutput.channels"]/constraints/options')
        for option in list(preserved_options):
            if int(option.text) in NEW:
                preserved_options.remove(option)
        # Ignore indentation left by removal; compare every element/attribute/value.
        def normalized(node):
            return (node.tag, sorted(node.attrib.items()), (node.text or '').strip(),
                    [normalized(child) for child in node])
        assert normalized(preserved) == normalized(old_tree), 'unrelated XML changed'
        for lang in languages:
            old = po_entries(baseline[PO.format(lang)])
            actual = dict(entries[lang])
            for label in NEW.values():
                assert '#' + label not in old
                actual.pop('#' + label)
            assert actual == old, f'pre-existing {lang} strings changed'
    return len(ids), {lang: len(data) for lang, data in entries.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--baseline', help='Optional Git revision for complete XML/PO preservation checks')
    args = parser.parse_args()
    root = args.root.resolve()
    languages = {lang: (root / PO.format(lang)).read_text() for lang in ('en_gb', 'de_de')}
    baseline = None
    if args.baseline:
        baseline = {path: subprocess.check_output(
            ['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
            for path in (XML, *[PO.format(lang) for lang in languages])}
    ids, strings = check((root / XML).read_text(), languages, baseline)
    print(json.dumps({'passed': True, 'setting_ids': ids, 'localized_contexts': strings,
                      'baseline': args.baseline,
                      'limits': 'Source XML/PO only; GUI and saved-file reload need device testing'}))


if __name__ == '__main__':
    main()
