from copy import deepcopy
import unittest
from process_observability.logging_change_groups import proposal_groups

class ChangeGroupControls(unittest.TestCase):
    def pair(self):
        a={'id':'a','kind':'add_event','operation_ids':['one'],'location':{'path':'warehouse.py','start_line':8,'end_line':8},
           'placement':'after','placement_context':{'status':'supported','point':{'line':8,'column':21},'path_conditions':[]},
           'what':'Первая причина','fields':[{'name':'key','origin':'existing','expression':'item.key'}],
           'logger':{'expression':'logger.info'}}
        b=deepcopy(a);b.update(id='b',operation_ids=['two'],what='Вторая причина')
        return [a,b]
    def test_same_point_keeps_all_ids_and_raw_claims(self):
        changes=self.pair();before=deepcopy(changes);groups=proposal_groups(changes)
        self.assertEqual(len(groups),1);self.assertEqual(groups[0]['change_ids'],['a','b'])
        self.assertEqual(groups[0]['operation_ids'],['one','two']);self.assertFalse(groups[0]['structured_conflict'])
        self.assertEqual(changes,before)
    def test_column_before_after_and_polarity_are_distinct(self):
        for kind in ('column','placement','guard'):
            changes=self.pair()
            if kind=='column':changes[1]['placement_context']['point']['column']=22
            if kind=='placement':changes[1]['placement']='before'
            if kind=='guard':changes[1]['placement_context']['path_conditions']=[{'condition':{'path':'warehouse.py','start_line':5,'end_line':5},'branch':'false'}]
            self.assertEqual(proposal_groups(changes),[],kind)
    def test_different_fields_or_receiver_are_flagged(self):
        changes=self.pair();changes[1]['fields'][0]['expression']='other.key'
        self.assertTrue(proposal_groups(changes)[0]['structured_conflict'])
        changes=self.pair();changes[1]['logger']['expression']='audit.info'
        self.assertTrue(proposal_groups(changes)[0]['structured_conflict'])
    def test_unsupported_point_is_explicit(self):
        changes=self.pair()
        for c in changes:c['placement_context']['status']='not_established'
        self.assertTrue(proposal_groups(changes)[0]['unverified_grouping'])

if __name__=='__main__':unittest.main()
