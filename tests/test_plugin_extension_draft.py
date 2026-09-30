"""Exercise non-scoring extensions independently of the deployed evaluator API."""
import copy
import json
import sys
import unittest
from pathlib import Path
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from scripts.build_plugin_extension_draft import build_manifest, build_facets, build_selection, examples, wrap_evaluator_v1
from trace_hunter.plugin_extension_preview import validate_manifest, validate_output, entity_key


class PluginExtensionDraftTests(unittest.TestCase):
    def setUp(self):
        self.examples = copy.deepcopy(examples())

    def test_published_drafts_match_builders_and_examples(self):
        for name, builder in [('plugin', build_manifest), ('facets', build_facets), ('selection', build_selection)]:
            actual = json.loads((ROOT / f'contracts/drafts/plugin-v2/{name}.schema.json').read_text())
            self.assertEqual(actual, builder())
            Draft202012Validator.check_schema(actual)
        for name, value in self.examples.items():
            self.assertEqual(value, json.loads((ROOT / f'examples/drafts/plugin-v2/{name}.json').read_text()))
            (validate_manifest if 'contributes' in value else validate_output)(value)

    def test_renderer_and_classification_do_not_need_metrics(self):
        for name in ('renderer-only', 'stage-classifier-suite'):
            value = self.examples[name]
            self.assertTrue(all('metrics' not in c for c in value['contributes']))
            validate_manifest(value)
        mixed = self.examples['stage-classifier-suite']['contributes']
        self.assertEqual([c['implementation']['host'] for c in mixed], ['remote_agent', 'browser', 'browser'])

    def test_only_evaluator_has_score_fields(self):
        pure = self.examples['renderer-only']
        pure['contributes'][0]['metrics'] = []
        with self.assertRaises(ValidationError):
            validate_manifest(pure)
        evaluator = self.examples['evaluator-compatibility']
        del evaluator['contributes'][0]['metrics']
        with self.assertRaises(ValidationError):
            validate_manifest(evaluator)

    def test_render_and_filter_cannot_silently_start_remote_computation(self):
        base = self.examples['stage-classifier-suite']
        for index, key, value in [(2, 'trigger', 'explicit'), (2, 'permissions', ['evaluation.submit']), (1, 'trigger', 'view')]:
            changed = copy.deepcopy(base)
            changed['contributes'][index][key] = value
            with self.assertRaises(ValidationError):
                validate_manifest(changed)
        base['contributes'][1]['implementation']['host'] = 'remote_agent'
        with self.assertRaises(ValidationError):
            validate_manifest(base)

    def test_duplicate_contributions_and_bad_default_config_rejected(self):
        value = self.examples['renderer-only']
        value['contributes'].append(copy.deepcopy(value['contributes'][0]))
        with self.assertRaisesRegex(ValueError, '贡献点'):
            validate_manifest(value)
        value['contributes'].pop()
        value['contributes'][0]['default_config'] = {'undeclared': True}
        with self.assertRaises(ValidationError):
            validate_manifest(value)

    def test_v1_projection_does_not_rewrite_deployed_identity(self):
        value = json.loads((ROOT / 'plugins/official/time/manifest.json').read_text())
        original = copy.deepcopy(value)
        projected = wrap_evaluator_v1(value)
        validate_manifest(projected)
        self.assertEqual(value, original)
        for field in ('plugin_id', 'version', 'package_digest'):
            self.assertEqual(projected[field], value[field])
        self.assertEqual(projected['contributes'][0]['metrics'], value['metrics'])

    def test_classifications_preserve_unknown_and_validate_members(self):
        value = self.examples['facets-partial']
        allowed = {entity_key(a['target']) for a in value['assignments']}
        validate_output(value, allowed)
        value['assignments'][1]['value_ids'] = ['explore']
        with self.assertRaises(ValidationError):
            validate_output(value, allowed)
        value['assignments'][1]['value_ids'] = []
        value['assignments'][0]['target']['entity_id'] = 'foreign'
        with self.assertRaisesRegex(ValueError, '输入范围'):
            validate_output(value, allowed)

    def test_facet_cardinality_references_and_coverage(self):
        original = self.examples['facets-partial']
        for values in [['undeclared'], ['explore', 'implement']]:
            value = copy.deepcopy(original)
            value['assignments'][0]['value_ids'] = values
            with self.assertRaises(ValueError):
                validate_output(value)
        value = copy.deepcopy(original)
        allowed = {entity_key(a['target']) for a in value['assignments']}
        value['coverage'] = 'complete'
        validate_output(value, allowed)
        value['assignments'].pop()
        with self.assertRaisesRegex(ValueError, '完整覆盖'):
            validate_output(value, allowed)

    def test_empty_slice_valid_but_duplicate_or_foreign_members_rejected(self):
        value = self.examples['selection-empty']
        validate_output(value, set())
        target = self.examples['facets-partial']['assignments'][0]['target']
        value['members'] = [target, target]
        with self.assertRaisesRegex(ValueError, '重复'):
            validate_output(value)
        value['members'] = [{**target, 'document_digest': '1' * 64}]
        with self.assertRaisesRegex(ValueError, '固定输入'):
            validate_output(value)


if __name__ == '__main__':
    unittest.main()
