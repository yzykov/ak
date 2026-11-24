import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import catalog_sync
import telegram_category_bot as bot
from catalog_sync_core.models import Item, Section
from catalog_sync_core.simple_plan import build_simple_plan


class CategoryApprovalTests(unittest.TestCase):
    def setUp(self):
        self.source = [Item('gkgross', 'https://gkgross.ru/one', 'Truck', source_id='one', category='Unknown'),
                       Item('gkgross', 'https://gkgross.ru/two', 'Other', source_id='two', category='Unknown')]
        self.tax = Mock()
        self.tax.sections = {42: Section(42, 'Trucks', 'trucks')}
        self.tax.excluded.return_value = False
        self.crawl = {'gkgross': {'complete': True, 'count': 2}}
        self.plan = build_simple_plan(self.source, [], self.tax, {}, self.crawl)
        self.registry = Mock()
        self.args = SimpleNamespace(apply=True, category_wait_seconds=900, config='config/catalog.json')
        self.env = {'TG_BOT_TOKEN': 'fake', 'TG_CHAT_ID': '123', 'TG_ADMIN_USER_ID': '456'}

    def test_register_before_launch_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, self.env), patch('catalog_sync.subprocess.run') as run:
            def launch(cmd, **kwargs):
                self.registry.set_pending_category.assert_called_once_with('gkgross', 'Unknown', 'Truck')
                self.assertEqual(kwargs['timeout'], 900)
                self.assertEqual(json.loads(Path(cmd[cmd.index('--pending-file') + 1]).read_text()), [['gkgross', 'unknown']])
                return SimpleNamespace(returncode=0)
            run.side_effect = launch
            self.assertTrue(catalog_sync.wait_for_categories(self.plan, self.source, self.registry, self.args, Path(folder)))

    def test_no_pending_or_no_apply_never_launches(self):
        with tempfile.TemporaryDirectory() as folder, patch('catalog_sync.subprocess.run') as run:
            self.args.apply = False
            self.assertFalse(catalog_sync.wait_for_categories(self.plan, self.source, self.registry, self.args, Path(folder)))
            self.args.apply = True
            self.plan.pending_category.clear()
            self.assertFalse(catalog_sync.wait_for_categories(self.plan, self.source, self.registry, self.args, Path(folder)))
            run.assert_not_called()
            self.registry.set_pending_category.assert_not_called()

    def test_timeout_and_failure_continue(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, self.env), patch('catalog_sync.subprocess.run') as run:
            for exc in (subprocess.TimeoutExpired('bot', 900), OSError()):
                run.side_effect = exc
                self.assertTrue(catalog_sync.wait_for_categories(self.plan, self.source, self.registry, self.args, Path(folder)))

    def test_missing_admin_does_not_run(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'TG_BOT_TOKEN':'fake','TG_CHAT_ID':'123','TG_ADMIN_USER_ID':''}), patch('catalog_sync.subprocess.run') as run:
            self.assertFalse(catalog_sync.wait_for_categories(self.plan, self.source, self.registry, self.args, Path(folder)))
            self.registry.set_pending_category.assert_called_once()
            run.assert_not_called()

    def test_scope_mapping_and_replan(self):
        self.registry.load_categories.return_value = {}
        self.registry.scan.return_value = {'pending/gkgross/abc123.json': {'source':'gkgross','category':'Unknown'},
                                           'pending/liftnet/abc123.json': {'source':'liftnet','category':'Unknown'}}
        scope = {('gkgross','unknown')}
        self.assertEqual(len(bot.pending_rows(self.registry, scope)), 1)
        self.assertIn('Код не найден', bot.process_command('/map liftnet:abc123 42', self.registry, self.tax, scope))
        self.assertIn('Текущая синхронизация', bot.process_command('/map gkgross:abc123 42', self.registry, self.tax, scope))
        self.registry.set_category.assert_called_once_with('gkgross','Unknown',42)
        self.registry.load_categories.return_value = {('gkgross','unknown'):42}
        self.assertEqual(bot.pending_rows(self.registry, scope), [])
        catalog_sync.apply_category_links(self.source, self.registry, self.registry.load_categories(), self.tax)
        replanned = build_simple_plan(self.source, [], self.tax, {}, self.crawl)
        self.assertEqual(len(replanned.create), 2)
        self.assertFalse(replanned.pending_category)

    def test_same_category_hash_distinguished_by_supplier(self):
        self.registry.load_categories.return_value = {}
        self.registry.scan.return_value = {'pending/gkgross/abc123.json': {'source':'gkgross','category':'Unknown'},
                                           'pending/liftnet/abc123.json': {'source':'liftnet','category':'Unknown'}}
        self.assertIn('неоднозначен', bot.process_command('/map abc123 42', self.registry, self.tax))
        self.assertIn('Сохранено', bot.process_command('/map liftnet:abc123 42', self.registry, self.tax))
        self.registry.set_category.assert_called_once_with('liftnet','Unknown',42)

    def test_scoped_bot_done_and_ignores_old_or_unauthorized_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            scope_file = Path(folder) / 'scope.json'
            scope_file.write_text(json.dumps([['gkgross','unknown']]))
            self.registry.load_categories.return_value = {}
            self.registry.scan.return_value = {'pending/gkgross/abc123.json': {'source':'gkgross','category':'Unknown'}}
            session = Mock()
            session.post.return_value.json.return_value = {'ok':True}
            def message(i, date, uid, text):
                return {'update_id': i, 'message': {'date':date,'chat':{'id':123},'from':{'id':uid},'text':text}}
            session.get.return_value.json.return_value = {'ok':True,'result':[
                message(1, 99, 456, '/map gkgross:abc123 42'),
                message(2, 100, 999, '/map gkgross:abc123 42'),
                message(3, 100, 456, '/done')]}
            config = {'target': {'api_base':'https://example.com','catalog_url':'https://example.com','excluded_section_codes':[]}}
            with patch.dict(os.environ, self.env), patch('telegram_category_bot.config_load',return_value=config), patch('telegram_category_bot.BitrixRegistry',return_value=self.registry), patch('telegram_category_bot._client'), patch('telegram_category_bot.requests.Session',return_value=session), patch('telegram_category_bot.time.time',return_value=100):
                self.assertEqual(bot.main(['--wait-seconds','900','--pending-file',str(scope_file)]), 0)
            self.registry.set_category.assert_not_called()
            self.registry.read.assert_not_called()
            self.registry.write.assert_not_called()
            self.assertEqual(session.post.call_count, 2)
            self.assertEqual(session.get.call_args.kwargs['params']['offset'], 0)


if __name__ == '__main__':
    unittest.main()
