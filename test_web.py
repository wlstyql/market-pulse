import json
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import collect

class WebTests(unittest.TestCase):
    def test_new_values_override_old(self):
        old={'values':{'usrate':3.0}, 'metadata':{'usrate':{'as_of':datetime.now(timezone.utc).isoformat()}}}
        new={'values':{'usrate':4.0}, 'metadata':{'usrate':{'as_of':datetime.now(timezone.utc).isoformat()}},'errors':{}}
        self.assertEqual(collect.merge(old,new)['values']['usrate'],4.0)
    def test_missing_recent_value_can_be_preserved_with_asof(self):
        now=datetime.now(timezone.utc).isoformat()
        old={'values':{'goldprice':5000},'metadata':{'goldprice':{'as_of':now}}}
        merged=collect.merge(old,{'values':{},'metadata':{},'errors':{}})
        self.assertEqual(merged['values']['goldprice'],5000)
        self.assertTrue(merged['metadata']['goldprice']['carried_forward'])
    def test_expired_prices_not_preserved(self):
        stamp=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
        old={'values':{'goldprice':5000},'metadata':{'goldprice':{'as_of':stamp}}}
        self.assertNotIn('goldprice',collect.merge(old,{'values':{},'metadata':{},'errors':{}})['values'])
    def test_empty_initial_json(self):
        d=json.loads((Path(__file__).parent/'docs'/'market_data.json').read_text())
        self.assertEqual(d['values'],{})
    def test_frontend_uses_static_json(self):
        html=(Path(__file__).parent/'docs'/'index.html').read_text()
        self.assertIn("fetch('./market_data.json?t='",html)
        self.assertNotIn("fetch('/api/market-data",html)

if __name__=='__main__': unittest.main()
