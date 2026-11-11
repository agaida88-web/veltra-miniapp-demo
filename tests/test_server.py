import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from decimal import Decimal
import unittest
class TestServer(unittest.TestCase):
    def test_tron_contract(self):
        assert server.tron_decode(server.USDT_CONTRACT)[0]==0x41
        with self.assertRaises(ValueError): server.tron_decode('T'+'0'*33)
    def test_amount(self):
        self.assertEqual(server.parse_amount('12.000001'),12000001)
        self.assertEqual(server.parse_amount('1.000000'),1000000)
        self.assertEqual(server.amt(1234567),'1.234567')
        with self.assertRaises(ValueError):server.parse_amount('0.5')
        with self.assertRaises(ValueError):server.parse_amount('1.0000001')
        with self.assertRaises(ValueError):server.parse_amount('-2')
    def test_password(self):
        p=server.hash_password('a_very_good_password_!')
        self.assertTrue(server.password_check('a_very_good_password_!',p))
        self.assertFalse(server.password_check('wrong',p))
if __name__=='__main__':unittest.main()
