"""Synthetic regression fixtures for corpus command labels, not captured user data."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('base_corpus',ROOT/'plugins/extensions/base-stages-1.2.0/classify.py')
classifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(classifier)


class BaseCorpusClassifierTests(unittest.TestCase):
    def span(self, command, name='lark-cli +record-list', **extra):
        return {'id':'s','kind':'tool','operation':'bash','name':name,'input':command,
                'agent_id':'main','parent_id':None,'phase_id':'task',**extra}

    def classify(self,s):return classifier.classify(s,{'s':s},{'task':'来源片段 messageIndex=31'})[0]

    def test_cli_named_tools_are_classified_from_actual_input(self):
        self.assertEqual(self.classify(self.span('cd /demo && lark-cli base +record-list --base-token example')), 'table')
        self.assertEqual(self.classify(self.span(json.dumps({'command':'lark-cli base +workflow-create'}))), 'workflow')
        self.assertIsNone(self.classify(self.span('echo "lark-cli base +record-list"')))
        self.assertIsNone(self.classify(self.span({'description':'lark-cli base +record-list'})))

    def test_help_does_not_become_a_create_or_update_action(self):
        command='lark-cli base +base-create --help 2>&1 | head -60'
        self.assertEqual(self.classify(self.span(command)), 'prepare')
        self.assertEqual(classifier.intent(classifier.cli_calls(command)[0]),'read')
        self.assertEqual(classifier.intent(classifier.cli_calls('lark-cli base +base-create --name demo')[0]),'create')
        self.assertEqual(classifier.intent(classifier.cli_calls('lark-cli base +field-create --name x')[0]),'update')
        self.assertEqual(classifier.intent(classifier.cli_calls('lark-cli base +form-detail')[0]),'read')

    def test_shared_execution_keeps_one_record_and_mixed_domain_is_unknown(self):
        command={'shared_execution':True,'commands':['lark-cli base +form-create','lark-cli base +workflow-create']}
        s=self.span(command)
        self.assertIsNone(self.classify(s))
        context={'input':{'run':{'id':'run'},'spans':[s],'phases':[{'id':'task','name':'task'}],'links':[]},
                 'input_digest':'0'*64,'input_ref':{'document_id':'run','document_digest':'0'*64,'selection_digest':'1'*64}}
        output=classifier.evaluate(context)
        assignments={a['facet_id']:a for a in output['data']['assignments']}
        self.assertEqual(len(output['data']['assignments']),3)
        self.assertEqual(assignments['base.intent']['value_ids'],['update'])
        self.assertEqual(assignments['base.cli']['value_ids'],['lark-cli base +form-create','lark-cli base +workflow-create'])
        self.assertEqual(assignments['base.stage']['status'],'unknown')

    def test_cli_parameter_values_never_become_labels(self):
        calls=classifier.cli_calls('lark-cli base +record-list --base-token hidden-token --query "test"')
        self.assertEqual(calls[0]['id'],'lark-cli base +record-list')
        self.assertEqual(classifier.cli_calls('echo lark-cli base +base-create'),[])
        self.assertEqual(classifier.cli_calls('lark-cli base record-get')[0]['id'],'lark-cli base record-get')

    def test_unrelated_filenames_are_not_skill_reads(self):
        s=self.span({'path':'/tmp/not-a-skill.md'},name='Read',operation='read')
        self.assertIsNone(self.classify(s))
