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
    def test_production_funds_are_disabled_before_provider_review(self):
        self.assertFalse(server.PRODUCTION_FUNDS_CONNECTED)
        self.assertFalse(server.PRODUCTION_BROKER_CONNECTED)
    def test_risk_banner_is_present(self):
        path=Path(__file__).resolve().parents[1]/'static'/'index.html'
        text=path.read_text(encoding='utf-8')
        self.assertIn('Торговля на финансовых рынках связана с высокими рисками', text)
        self.assertIn('НЕ последние, НЕ заемные, НЕ кредитные',text)
        self.assertIn('Пополнение USDT TRC20',text)
    def test_account_view_status_elements_present(self):
        path=Path(__file__).resolve().parents[1]/'static'/'index.html'
        markup=path.read_text(encoding='utf-8')
        for element_id in ('accountConnectionStatus','profileBroker','profileTradingAccount',
                           'profileFundingStatus','depositAvailability'):
            self.assertIn('id="'+element_id+'"', markup)
    def test_instruction_menu_and_risk_disclosures(self):
        root=Path(__file__).resolve().parents[1]/'static'
        markup=(root/'index.html').read_text(encoding='utf-8')
        script=(root/'app.js').read_text(encoding='utf-8')
        css=(root/'instructions.css').read_text(encoding='utf-8')
        self.assertIn('id="instructionPage"',markup)
        self.assertIn('id="headerInstructions"',markup)
        self.assertIn('id="sideInstructions"',markup)
        self.assertIn('id="instructionBack"',markup)
        self.assertEqual(markup.count('class="instruction-item"'),6)
        self.assertIn('XAU/AUD',markup)
        self.assertIn('2,5%',markup)
        self.assertIn('Это параметр модели, а не гарантированная доходность',markup)
        self.assertIn('не предусматривает реферальных вознаграждений',markup)
        self.assertIn('с 02:00 до 23:59',markup)
        self.assertIn('10 дней',markup)
        self.assertIn('подключению реального торгового счёта и кошелька',markup)
        self.assertIn('платёжные операции в приложении не активированы',markup)
        self.assertIn('function showInstructions()',script)
        self.assertIn('function closeInstructions()',script)
        self.assertIn('instruction-hero',css)
    def test_password(self):
        p=server.hash_password('a_very_good_password_!')
        self.assertTrue(server.password_check('a_very_good_password_!',p))
        self.assertFalse(server.password_check('wrong',p))
if __name__=='__main__':unittest.main()
