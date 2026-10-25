"""Queued acceptance is not completion: exercise the durable reply round trip."""
import asyncio
import sqlite3
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from swarmly.telegram.adapters.base import AdapterReply, format_channel_prompt
from swarmly.telegram.adapters.codex import CodexThreadAdapter
from swarmly.telegram.channel import _ReceiptSuperseded
from swarmly.telegram.cli import build_parser, _reply
from swarmly.telegram.replies import ReplyStore, reply_state_path
from swarmly.telegram.wire import Envelope, MessageType, format_message
from tests.test_telegram_channel import make_config, make_channel, event, run, GROUP, ADMIN_ID


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(
            agent={'alias': 'Codex', 'name': 'example_codex_bot'},
            adapter={'type': 'codex_thread', 'conversation': 'current'},
            peers={'Hermes': 'example_hermes_bot'},
        )
        self.config.source_path = Path(self.tmp.name) / 'agent.local.yaml'
        self.store = ReplyStore(reply_state_path(self.config))

    def ticket(self, mid=1, now=100):
        return self.store.register(chat_id=GROUP, message_id=mid, recipient='example_hermes_bot',
                                   text='@example_codex_bot please review #17', timeout=10, now=now)[0]

    def channel(self, adapter=None, **kwargs):
        adapter = adapter or CodexThreadAdapter('Codex', self.config.adapter)
        return make_channel(self.config, adapter=adapter, **kwargs)

    def test_restart_retains_pending_and_deduplicates_inbound(self):
        row = self.ticket()
        self.store.queued(row['id'])
        restarted = ReplyStore(self.store.path)
        duplicate, created = restarted.register(chat_id=GROUP, message_id=1,
            recipient='example_hermes_bot', text='duplicate', now=100)
        self.assertFalse(created)
        self.assertEqual(duplicate['id'], row['id'])
        self.assertEqual(restarted.summary(), {'queued': 1})

    def unposted(self):
        row = self.ticket()
        self.store.queued(row['id'])
        self.store.submit(row['id'], text='Review posted on #17.')
        self.store.claim_ready()
        self.store.unposted(row['id'], 'answer not posted: Swarm pulse is transport-only')
        return row

    def test_an_answer_that_is_only_no_reply_is_refused_at_submission(self):
        """The transport never posts the sentinel: accepted, it left the ticket `claimed`
        with nothing to send (#321 review)."""
        row = self.ticket()
        self.store.queued(row['id'])
        for text in ('NO_REPLY', '@example_hermes_bot: NO_REPLY', '**NO_REPLY**'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, '--skip'):
                self.store.submit(row['id'], text=text)
        self.assertEqual(self.store.get(row['id'])['state'], 'queued')
        self.assertEqual(self.store.submit(row['id'], text='NO_REPLY\nLGTM, merged.')['text'],
                         '@example_hermes_bot: NO_REPLY\nLGTM, merged.')

    def test_an_unposted_ticket_says_so_when_answered_again(self):
        row = self.unposted()
        with self.assertRaisesRegex(ValueError, 'unposted .*transport-only'):
            self.store.submit(row['id'], text='a second answer')

    def test_the_author_can_close_an_unposted_ticket(self):
        row = self.unposted()
        self.assertEqual(self.store.resolve_prefix(row['id'][:8], author='example_hermes_bot'), 1)
        self.assertEqual(self.store.get(row['id'])['state'], 'skipped')
        self.assertEqual(self.store.overdue(now=100), [])

    def test_reply_submissions_are_addressed_and_idempotent(self):
        row = self.ticket()
        result = self.store.submit(row['id'], text='Review posted on #17.')
        self.assertEqual(result['text'], '@example_hermes_bot: Review posted on #17.')
        self.store.submit(row['id'], text='Review posted on #17.')
        self.assertEqual(self.store.claim_ready()['id'], row['id'])
        self.store.attempting(row['id'])
        self.store.sent(row['id'], 42)
        self.assertEqual(self.store.submit(row['id'], text='Review posted on #17.')['state'], 'sent')
        self.assertIsNone(self.store.claim_ready())
        with self.assertRaises(ValueError):
            self.store.submit(row['id'], text='different')

    def test_unknown_empty_and_oversize_replies_are_rejected(self):
        row = self.ticket()
        for ticket, text in [('unknown', 'hello'), (row['id'], ''), (row['id'], 'x' * 500)]:
            with self.assertRaises(ValueError):
                self.store.submit(ticket, text=text)

    def test_missing_message_id_is_rejected_before_a_duplicate_can_be_created(self):
        with self.assertRaisesRegex(ValueError, 'message_id'):
            self.store.register(chat_id=GROUP, message_id=None, recipient='peer', text='cannot correlate')

    def test_pure_ack_can_be_resolved_without_posting(self):
        row = self.ticket()
        self.store.submit(row['id'], skip_reason='Pure acknowledgment; no action requested')
        self.assertIsNone(self.store.claim_ready())
        self.assertEqual(self.store.overdue(now=200), [])
        with self.assertRaises(ValueError):
            self.store.submit(row['id'], text='must not echo an ACK')

    def test_receipt_reservation_is_atomic_and_idempotent(self):
        row = self.ticket()
        self.store.queued(row['id'])
        self.assertTrue(self.store.claim_receipt(row['id']))
        self.assertFalse(self.store.claim_receipt(row['id']))
        self.store.submit(row['id'], skip_reason='skip arrived after receipt reservation')
        final = self.store.get(row['id'])
        self.assertEqual(final['state'], 'skipped')
        self.assertEqual(final['receipt_reserved'], 1)
        self.assertEqual(final['receipt_state'], 'attempted')

    def test_duplicate_receipt_reservation_reports_the_reservation_state(self):
        row = self.ticket(mid=2)
        self.store.queued(row['id'])
        self.assertTrue(self.store.claim_receipt(row['id']))
        with self.assertRaisesRegex(_ReceiptSuperseded, r'already reserved \(attempted\)'):
            self.channel()._assert_receipt_pending(row['id'])

    def test_receipt_claim_state_matrix(self):
        queued = self.ticket(mid=10)
        self.store.queued(queued['id'])
        self.assertTrue(self.store.claim_receipt(queued['id']))
        self.assertFalse(self.store.claim_receipt(queued['id']))

        overdue = self.ticket(mid=11)
        self.store.queued(overdue['id'])
        self.store.overdue(now=200)
        self.assertEqual(self.store.get(overdue['id'])['state'], 'overdue')
        self.assertTrue(self.store.claim_receipt(overdue['id']))

        skipped = self.ticket(mid=12)
        self.store.submit(skipped['id'], skip_reason='informational status')
        self.assertFalse(self.store.claim_receipt(skipped['id']))

        sent = self.ticket(mid=13)
        self.store.submit(sent['id'], text='done')
        claimed = self.store.claim_ready()
        self.store.attempting(claimed['id'])
        self.store.sent(claimed['id'], 13)
        self.assertFalse(self.store.claim_receipt(sent['id']))

    def test_receipt_failure_is_unknown_and_recovery_alerts_it(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        channel = self.channel(adapter)
        channel._sender = AsyncMock(side_effect=OSError('network lost'))
        with self.assertRaises(OSError):
            run(channel.handle_event(event('@example_codex_bot hello', username='example_hermes_bot')))
        row, created = self.store.register(chat_id=GROUP, message_id=1,
                                           recipient='example_hermes_bot', text='duplicate')
        self.assertFalse(created)
        self.assertEqual(row['receipt_state'], 'unknown')
        self.store.recover()
        alert = self.store.overdue()[0]
        self.assertEqual(alert['receipt_state'], 'unknown')
        self.assertEqual(alert['alert_kind'], 'receipt_failure')

    def test_receipt_alert_does_not_hide_task_deadline_alert(self):
        row = self.ticket(now=100)
        self.store.queued(row['id'])
        self.assertTrue(self.store.claim_receipt(row['id']))
        self.store.receipt_unknown(row['id'], 'network lost')
        alerts = self.store.overdue(now=200)
        self.assertEqual({alert['alert_kind'] for alert in alerts}, {'task_overdue', 'receipt_failure'})
        self.store.receipt_notified(row['id'])
        remaining = self.store.overdue(now=200)
        self.assertEqual([alert['alert_kind'] for alert in remaining], ['task_overdue'])

    def test_receipt_failure_alert_includes_detail_and_uses_receipt_flag(self):
        row = self.ticket(now=time.time() + 100)
        self.store.queued(row['id'])
        self.assertTrue(self.store.claim_receipt(row['id']))
        self.store.receipt_unknown(row['id'], 'network lost after Telegram accepted')
        channel = self.channel()
        run(channel.flush_replies())
        notices = [item['text'] for item in channel._test_sender.sent]
        receipt_notices = [notice for notice in notices if 'receipt delivery failed' in notice]
        self.assertEqual(len(receipt_notices), 1)
        self.assertIn('network lost after Telegram accepted', receipt_notices[0])
        self.assertNotIn(' queued for message ', receipt_notices[0])
        self.assertEqual(self.store.get(row['id'])['receipt_notified'], 1)
        self.assertEqual(self.store.get(row['id'])['notified'], 0)

    def test_late_task_resolution_does_not_hide_receipt_failure(self):
        row = self.ticket(now=time.time() + 100)
        self.store.queued(row['id'])
        self.assertTrue(self.store.claim_receipt(row['id']))
        self.store.receipt_unknown(row['id'], 'receipt outcome is uncertain')
        self.store.submit(row['id'], text='late task answer')
        alerts = self.store.overdue()
        self.assertEqual([alert['alert_kind'] for alert in alerts], ['receipt_failure'])

    def test_legacy_resolved_receipt_reservation_is_not_realerted(self):
        legacy = Path(self.tmp.name) / 'legacy.sqlite3'
        db = sqlite3.connect(legacy)
        try:
            db.execute("""CREATE TABLE replies (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER,
                recipient TEXT NOT NULL, original TEXT NOT NULL, created REAL NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '', telegram_message_id INTEGER,
                notified INTEGER NOT NULL DEFAULT 0,
                receipt_reserved INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_id, message_id))""")
            values = ('legacy', GROUP, 9, 'peer', 'old', 100, 110, '', '', None, 0, 1)
            for index, state in enumerate(('sent', 'skipped', 'queued'), start=9):
                db.execute("INSERT INTO replies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (f'{state}-{values[0]}', GROUP, index, *values[3:7], state, *values[7:]))
            db.commit()
        finally:
            db.close()
        migrated = ReplyStore(legacy)
        self.assertEqual(migrated.get('sent-legacy')['receipt_state'], 'sent')
        self.assertEqual(migrated.get('skipped-legacy')['receipt_state'], 'sent')
        self.assertEqual(migrated.get('queued-legacy')['receipt_state'], 'unknown')

    def test_pre_receipt_schema_migrates_without_losing_rows(self):
        legacy = Path(self.tmp.name) / 'pre-receipt.sqlite3'
        db = sqlite3.connect(legacy)
        try:
            db.execute("""CREATE TABLE replies (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER,
                recipient TEXT NOT NULL, original TEXT NOT NULL, created REAL NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '', telegram_message_id INTEGER,
                notified INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_id, message_id))""")
            db.execute("""INSERT INTO replies
                (id, chat_id, message_id, recipient, original, created, deadline, state, text, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       ('legacy', GROUP, 77, 'peer', 'old request', 100, 200, 'queued', '', ''))
            db.commit()
        finally:
            db.close()

        migrated = ReplyStore(legacy)
        row = migrated.get('legacy')
        self.assertEqual(row['original'], 'old request')
        self.assertEqual(row['receipt_reserved'], 0)
        self.assertEqual(row['receipt_state'], 'none')
        self.assertEqual(row['receipt_detail'], '')
        self.assertEqual(row['receipt_notified'], 0)
        self.assertIsNone(row['receipt_message_id'])
        reopened = ReplyStore(legacy)
        self.assertEqual(reopened.get('legacy')['state'], 'queued')

    def test_pre_receipt_schema_migration_is_safe_for_concurrent_openers(self):
        legacy = Path(self.tmp.name) / 'concurrent-pre-receipt.sqlite3'
        db = sqlite3.connect(legacy)
        try:
            db.execute("""CREATE TABLE replies (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER,
                recipient TEXT NOT NULL, original TEXT NOT NULL, created REAL NOT NULL,
                deadline REAL NOT NULL, state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '', telegram_message_id INTEGER,
                notified INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chat_id, message_id))""")
            db.commit()
        finally:
            db.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            stores = list(pool.map(lambda _: ReplyStore(legacy), range(4)))
        self.assertEqual(len(stores), 4)
        db = sqlite3.connect(legacy)
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(replies)")}
        finally:
            db.close()
        self.assertTrue({
            'receipt_reserved', 'receipt_state', 'receipt_detail',
            'receipt_notified', 'receipt_message_id',
        }.issubset(columns))

    def test_overdue_alert_once_and_late_reply_allowed(self):
        row = self.ticket()
        self.store.queued(row['id'])
        channel = self.channel()
        run(channel.flush_replies())
        self.assertEqual(len(channel._test_sender.sent), 1)
        self.assertIn('overdue', channel._test_sender.last)
        self.assertIn('@example_hermes_bot', channel._test_sender.last)
        run(channel.flush_replies())
        self.assertEqual(len(channel._test_sender.sent), 2)  # reminder within the cap (#49)
        run(channel.flush_replies())
        self.assertEqual(len(channel._test_sender.sent), 2)  # cap reached: silent from here
        self.store.submit(row['id'], text='Blocked on access to #17.')
        run(channel.flush_replies())
        self.assertEqual(self.store.get(row['id'])['state'], 'sent')
        self.assertEqual(channel._test_sender.sent[-1]['reply_to'], 1)

    def test_uncertain_send_is_retained_not_automatically_retried(self):
        row = self.ticket()
        self.store.submit(row['id'], text='result')
        channel = self.channel()
        channel._sender = AsyncMock(side_effect=OSError('connection lost'))
        with self.assertRaises(OSError):
            run(channel.flush_replies())
        self.assertEqual(self.store.get(row['id'])['state'], 'delivery_unknown')
        self.assertIsNone(self.store.claim_ready())
        with self.assertRaises(ValueError):
            self.store.submit(row['id'], text='result')

    def test_restart_during_send_does_not_duplicate(self):
        row = self.ticket()
        self.store.submit(row['id'], text='result')
        self.store.claim_ready()
        self.store.attempting(row['id'])
        restarted = ReplyStore(self.store.path)
        restarted.recover()
        self.assertEqual(restarted.get(row['id'])['state'], 'delivery_unknown')
        self.assertIsNone(restarted.claim_ready())

    def test_multirecipient_prompt_preserves_exact_message_and_reply_contract(self):
        text = '@example_codex_bot @example_agy_bot — process is now formal.\n\nPlease comment on #17.'
        completed = subprocess.CompletedProcess([], 0, 'queued', '')
        channel = self.channel()
        with patch('swarmly.telegram.adapters.codex.subprocess.run', return_value=completed) as queue:
            run(channel.handle_event(event(text, username='example_hermes_bot')))
        prompt = queue.call_args.args[0][-1]
        self.assertIn(text, prompt)
        self.assertIn('This was routed to YOU (@example_codex_bot)', prompt)
        self.assertIn('local final answer is NOT sent to Telegram', prompt)
        self.assertIn('Mixed status plus a request still requires a reply', prompt)
        self.assertIn('receipt only; reply pending', channel._test_sender.last)
        self.assertEqual(self.store.summary(), {'queued': 1})

    def test_explicit_note_does_not_create_a_reply_obligation(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        channel = self.channel(adapter)
        note = '@example_codex_bot ' + format_message(
            Envelope(type=MessageType.NOTE, to='Codex', intent='FYI', id='n-1'),
            'PR #35 is merged; no action is requested.')
        self.assertIsNone(run(channel.handle_event(event(note, username='example_hermes_bot'))))
        adapter.respond.assert_not_awaited()
        self.assertEqual(self.store.summary(), {})

    def test_duplicate_event_never_queues_or_receipts_twice(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        channel = self.channel(adapter)
        async def scenario():
            incoming = event('@example_codex_bot please review #17', username='example_hermes_bot')
            await channel.handle_event(incoming)
            await channel.handle_event(incoming)
        run(scenario())
        self.assertEqual(adapter.respond.await_count, 1)
        self.assertEqual(len(channel._test_sender.sent), 1)

    def test_concurrent_addressed_events_are_not_dropped(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        seen = []
        async def respond(message):
            seen.append(message.message_id)
            await asyncio.sleep(0)
            return AdapterReply(meta={'queued': True})
        adapter.respond = respond
        channel = self.channel(adapter)
        async def scenario():
            await asyncio.gather(*[channel.handle_event(event('@example_codex_bot please reply', message_id=i)) for i in (1, 2, 3)])
        run(scenario())
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(self.store.summary(), {'queued': 3})

    def test_queue_failure_cannot_be_mistaken_for_acceptance(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(ok=False, error='queue failed'))
        channel = self.channel(adapter)
        run(channel.handle_event(event('@example_codex_bot please review', username='example_hermes_bot')))
        self.assertEqual(self.store.summary(), {'queue_failed': 1})
        self.assertNotIn('queued in active task', channel._test_sender.last)

    def test_real_sender_receipt_and_mentions_use_plain_entity_offsets(self):
        row = self.ticket()
        self.store.submit(row['id'], text='Checks < 5 & done.')
        channel = self.channel()
        channel._sender = None
        channel.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=321)))
        run(channel.flush_replies())
        self.assertEqual(self.store.public_status(row['id'])['telegram_message_id'], 321)
        args = channel.bot.send_message.call_args.kwargs
        self.assertIsNone(args['parse_mode'])
        self.assertIn('Checks < 5 & done.', args['text'])
        self.assertTrue(args['entities'])
        self.assertEqual(args['reply_to_message_id'], 1)

    def test_receipt_records_the_message_id_returned_by_its_own_send(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        channel = self.channel(adapter)

        async def sender(payload, reply_to_message_id=None):
            return SimpleNamespace(message_id=322)

        channel._sender = sender
        run(channel.handle_event(event('@example_codex_bot please review', username='example_hermes_bot')))
        row = self.store.register(chat_id=GROUP, message_id=1,
                                  recipient='example_hermes_bot', text='duplicate')[0]
        self.assertEqual(row['receipt_state'], 'sent')
        self.assertEqual(row['receipt_message_id'], 322)

    def test_cli_distinguishes_submission_from_telegram_acceptance(self):
        row = self.ticket()
        parser = build_parser()
        args = parser.parse_args(['--config', str(self.config.source_path), 'reply', row['id'], '--text', 'done'])
        with patch('builtins.print'):
            self.assertEqual(_reply(self.config, args), 3)
            run(self.channel().flush_replies())
            self.assertEqual(_reply(self.config, args), 0)

    def test_private_reply_and_overdue_alert_never_go_to_group(self):
        row, _ = self.store.register(chat_id=123, message_id=1, recipient='maintainer',
                                     text='private request', timeout=1, now=100)
        self.store.queued(row['id'])
        channel = self.channel()
        channel._sender = None
        channel.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1234)))
        run(channel.flush_replies())
        self.store.submit(row['id'], text='private response')
        run(channel.flush_replies())
        self.assertEqual([call.kwargs['chat_id'] for call in channel.bot.send_message.call_args_list], [123, 123])

    def test_cancellation_during_pacing_keeps_unsent_reply_retryable(self):
        row = self.ticket()
        self.store.submit(row['id'], text='answer')
        async def scenario():
            sleeping = asyncio.Event()
            async def slow_pacing(*_):
                sleeping.set()
                await asyncio.Event().wait()
            channel = self.channel()
            channel._pace = slow_pacing
            flush = asyncio.create_task(channel.flush_replies())
            await sleeping.wait()
            self.assertEqual(self.store.get(row['id'])['state'], 'claimed')
            flush.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await flush
            self.assertEqual(channel._test_sender.sent, [])
        run(scenario())
        self.store.recover()
        self.assertEqual(self.store.get(row['id'])['state'], 'ready')
        run(self.channel().flush_replies())
        self.assertEqual(self.store.get(row['id'])['state'], 'sent')

    def test_waiting_inbound_is_durable_before_previous_receipt_finishes(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        async def scenario():
            entered = asyncio.Event()
            async def block_send(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            channel = self.channel(adapter)
            channel.send = block_send
            first = asyncio.create_task(channel.handle_event(event('@example_codex_bot please act', message_id=1)))
            await entered.wait()
            second = asyncio.create_task(channel.handle_event(event('@example_codex_bot please act', message_id=2)))
            await asyncio.sleep(0)
            self.assertEqual(self.store.summary(), {'pending': 1, 'queued': 1})
            first.cancel(); second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
        run(scenario())
        self.store.recover()
        self.assertEqual(self.store.summary(), {'overdue': 1, 'queued': 1})

    def test_late_reply_failure_gets_a_new_alert(self):
        row = self.ticket()
        self.store.queued(row['id'])
        channel = self.channel()
        run(channel.flush_replies())
        self.assertEqual(self.store.get(row['id'])['notified'], 1)
        self.store.submit(row['id'], text='late answer')
        channel._sender = AsyncMock(side_effect=OSError('offline'))
        with self.assertRaises(OSError):
            run(channel.flush_replies())
        self.assertEqual(self.store.overdue()[0]['state'], 'delivery_unknown')

    def test_terminal_ack_is_filtered_before_queue(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        channel = self.channel(adapter)
        run(channel.handle_event(event('@example_codex_bot acknowledged', username='example_hermes_bot')))
        self.assertEqual(channel._test_sender.sent, [])
        adapter.respond.assert_not_awaited()
        self.assertEqual(self.store.summary(), {})

    def test_skip_during_receipt_pacing_suppresses_stale_pending_receipt(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        adapter.respond = AsyncMock(return_value=AdapterReply(meta={'queued': True}))
        channel = self.channel(adapter)
        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()
            async def slow_pacing(*_):
                entered.set()
                await release.wait()
            channel._pace = slow_pacing
            task = asyncio.create_task(channel.handle_event(
                event('@example_codex_bot please review', username='example_hermes_bot')))
            await entered.wait()
            # The event's ticket is recovered by the unique chat/message key.
            row, created = self.store.register(chat_id=GROUP, message_id=1,
                                               recipient='example_hermes_bot', text='duplicate')
            self.assertFalse(created)
            self.store.submit(row['id'], skip_reason='pure ACK, no action')
            release.set()
            result = await task
            self.assertIsNone(result)
        run(scenario())
        self.assertFalse(any('reply pending' in row['text'] for row in channel._test_sender.sent))
        self.assertEqual(channel.stats.handled, 1)

    def test_fast_task_answer_still_sends_protocol_ack(self):
        adapter = CodexThreadAdapter('Codex', self.config.adapter)
        async def answer_immediately(message):
            ticket_id = message.response_instructions.split(']')[0].split()[-1]
            self.store.submit(ticket_id, text='handled immediately')
            return AdapterReply(meta={'queued': True})
        adapter.respond = answer_immediately
        channel = self.channel(adapter)
        request = '@example_codex_bot ' + format_message(
            Envelope(type=MessageType.REQUEST, to='Codex', intent='REVIEW', id='r-fast'),
            'please review')
        run(channel.handle_event(event(request, username='example_hermes_bot')))
        self.assertTrue(any('[A2A v1.1] ACK' in row['text'] for row in channel._test_sender.sent))
        self.assertFalse(any('reply pending' in row['text'] for row in channel._test_sender.sent))

    def test_one_failed_overdue_alert_does_not_starve_later_tickets(self):
        first, _ = self.store.register(chat_id=GROUP, message_id=1, recipient='peer',
                                       text='first', timeout=0)
        second, _ = self.store.register(chat_id=GROUP, message_id=2, recipient='peer',
                                        text='second', timeout=0)
        self.store.queued(first['id'])
        self.store.queued(second['id'])
        calls = []
        async def sender(payload, reply_to_message_id=None):
            calls.append(reply_to_message_id)
            if reply_to_message_id == 1:
                raise RuntimeError('Bad Request: message to be replied not found')
        channel = self.channel()
        channel._sender = sender
        run(channel.flush_replies())
        self.assertEqual(calls, [1, None])
        run(channel.flush_replies())
        self.assertEqual(calls, [1, None, 1, None])  # reminded once more within the cap (#49)
        run(channel.flush_replies())
        self.assertEqual(calls, [1, None, 1, None, 2])  # capped; the second ticket finally alerts
        self.assertEqual(self.store.get(first['id'])['notified'], 1)
        self.assertEqual(self.store.get(second['id'])['notified'], 1)

    def test_ready_reply_preempts_an_overdue_alert_backlog(self):
        first, _ = self.store.register(chat_id=GROUP, message_id=10, recipient='peer',
                                       text='first', timeout=0)
        second, _ = self.store.register(chat_id=GROUP, message_id=11, recipient='peer',
                                        text='second', timeout=0)
        ready, _ = self.store.register(chat_id=GROUP, message_id=12, recipient='peer',
                                       text='answer', timeout=600)
        self.store.queued(first['id'])
        self.store.queued(second['id'])
        calls = []

        async def sender(payload, reply_to_message_id=None):
            calls.append((payload, reply_to_message_id))
            if reply_to_message_id == 10:
                self.store.submit(ready['id'], text='substantive answer')

        channel = self.channel()
        channel._sender = sender
        run(channel.flush_replies())
        self.assertEqual(len(calls), 1)
        run(channel.flush_replies())
        self.assertEqual(calls[1][1], 12)
        self.assertEqual(self.store.get(ready['id'])['state'], 'sent')

    def test_explicit_state_path_without_config_source_is_rejected_early(self):
        self.config.adapter.reply_state_path = str(self.store.path)
        self.config.source_path = None
        with self.assertRaisesRegex(ValueError, 'config.source_path'):
            self.channel()


class Issue49AlertDisciplineTests(unittest.TestCase):
    """#49: alert caps, digest alerts, self-serve CLOSED resolution, freeze persistence."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = make_config(
            agent={'alias': 'Codex', 'name': 'example_codex_bot'},
            adapter={'type': 'codex_thread', 'conversation': 'current'},
            peers={'Hermes': 'example_hermes_bot'},
        )
        self.config.source_path = Path(self.tmp.name) / 'agent.local.yaml'
        self.store = ReplyStore(reply_state_path(self.config))

    def ticket(self, mid=1, now=100, timeout=10):
        row = self.store.register(chat_id=GROUP, message_id=mid, recipient='example_hermes_bot',
                                  text='probe message', timeout=timeout, now=now)[0]
        self.store.queued(row['id'])
        return row

    def channel(self):
        channel = make_channel(self.config, adapter=CodexThreadAdapter('Codex', self.config.adapter))
        channel.adapter.respond = AsyncMock(return_value=AdapterReply(text='adapter answered'))
        return channel

    def test_overdue_alerts_are_capped_per_ticket(self):
        row = self.ticket()
        self.assertEqual([r['id'] for r in self.store.overdue(now=200, alert_cap=2)], [row['id']])
        self.store.notified(row['id'])
        self.assertEqual([r['id'] for r in self.store.overdue(now=200, alert_cap=2)], [row['id']])
        self.store.notified(row['id'])
        self.assertEqual(self.store.overdue(now=200, alert_cap=2), [])

    def test_freeze_pauses_the_alert_outbox(self):
        self.ticket()
        channel = self.channel()
        channel._frozen = True
        run(channel.flush_replies())
        self.assertEqual(channel._test_sender.sent, [])

    def test_many_open_tickets_for_one_recipient_send_one_digest(self):
        rows = [self.ticket(mid=mid) for mid in (1, 2, 3)]
        channel = self.channel()
        run(channel.flush_replies())
        sent = channel._test_sender.sent
        self.assertEqual(len(sent), 1)
        self.assertIn('3 reply tickets overdue', sent[0]['text'])
        for row in rows:
            self.assertIn(row['id'][:8], sent[0]['text'])
        # The cap allows one more alert round, then the group goes quiet (#49.B).
        self.assertEqual(len(self.store.overdue(now=200)), 3)
        run(channel.flush_replies())
        self.assertEqual(len(channel._test_sender.sent), 2)
        self.assertEqual(self.store.overdue(now=200), [])

    def test_a_single_overdue_ticket_keeps_the_classic_alert(self):
        self.ticket()
        channel = self.channel()
        run(channel.flush_replies())
        sent = channel._test_sender.sent
        self.assertEqual(len(sent), 1)
        self.assertIn('intent=OVERDUE', sent[0]['text'])
        self.assertIn('maintainer attention required', sent[0]['text'])

    def test_closed_note_from_the_author_resolves_the_ticket(self):
        row = self.ticket()
        channel = self.channel()
        note = f"@HermesBot [A2A v1.1] NOTE to=Codex intent=CLOSED ref={row['id'][:8]}\n\nno reply needed"
        self.assertIsNone(run(channel.handle_event(
            event(note, username='example_hermes_bot', message_id=99))))
        self.assertEqual(self.store.get(row['id'])['state'], 'skipped')
        self.assertEqual(self.store.overdue(now=200), [])
        channel.adapter.respond.assert_not_called()

    def test_closed_note_from_another_author_does_not_resolve(self):
        row = self.ticket()
        channel = self.channel()
        note = f"@HermesBot [A2A v1.1] NOTE to=Codex intent=CLOSED ref={row['id'][:8]}\n\nnot mine"
        run(channel.handle_event(event(note, username='AntigravityBot', message_id=98)))
        self.assertEqual(self.store.get(row['id'])['state'], 'queued')

    def test_closed_note_also_resolves_queue_failed_tickets(self):
        """Live gap: a broken queue row must stay closable by its originating author."""
        row = self.store.register(chat_id=GROUP, message_id=11, recipient='example_hermes_bot',
                                  text='probe message', timeout=10, now=100)[0]
        self.store.queue_failed(row['id'], 'queue submission failed')
        self.assertEqual(self.store.get(row['id'])['state'], 'queue_failed')
        self.assertEqual(self.store.resolve_prefix(row['id'][:8], author='example_hermes_bot'), 1)
        self.assertEqual(self.store.get(row['id'])['state'], 'skipped')
        self.assertEqual(self.store.overdue(now=200), [])

    def test_closed_note_from_another_author_does_not_resolve_queue_failed(self):
        row = self.store.register(chat_id=GROUP, message_id=12, recipient='example_hermes_bot',
                                  text='probe message', timeout=10, now=100)[0]
        self.store.queue_failed(row['id'], 'queue submission failed')
        self.assertEqual(self.store.resolve_prefix(row['id'][:8], author='AntigravityBot'), 0)
        self.assertEqual(self.store.get(row['id'])['state'], 'queue_failed')

    def test_freeze_persists_across_restart_and_resumes_cleanly(self):
        channel = self.channel()
        freeze = "@HermesBot [A2A v1.1] STATUS to=* intent=FREEZE ref=window-1"
        run(channel.handle_event(event(freeze, username='admin', author_id=ADMIN_ID,
                                       is_bot=False, message_id=200)))
        self.assertTrue(channel._frozen)
        state_path = Path(self.tmp.name) / 'agent.local.freeze.json'
        self.assertTrue(state_path.exists())

        restored = self.channel()
        run(restored._restore_freeze_state())
        self.assertTrue(restored._frozen)
        self.assertTrue(any('intent=FROZEN' in s['text'] for s in restored._test_sender.sent))

        resume = "@HermesBot [A2A v1.1] STATUS to=* intent=RESUME ref=window-2"
        run(restored.handle_event(event(resume, username='admin', author_id=ADMIN_ID,
                                        is_bot=False, message_id=201)))
        self.assertFalse(restored._frozen)
        self.assertFalse(state_path.exists())

        clean = self.channel()
        run(clean._restore_freeze_state())
        self.assertFalse(clean._frozen)
        self.assertEqual(clean._test_sender.sent, [])

    def test_digest_is_scoped_by_chat_and_recipient(self):
        """#51.N1: tickets for one recipient in different chats do not merge into one cross-chat digest."""
        chat_a = GROUP
        chat_b = -1009999999999
        row_a1 = self.store.register(chat_id=chat_a, message_id=1, recipient='example_hermes_bot',
                                     text='msg a1', timeout=10, now=100)[0]
        self.store.queued(row_a1['id'])
        row_a2 = self.store.register(chat_id=chat_a, message_id=2, recipient='example_hermes_bot',
                                     text='msg a2', timeout=10, now=100)[0]
        self.store.queued(row_a2['id'])

        rows_b = []
        for mid in (10, 11, 12):
            r = self.store.register(chat_id=chat_b, message_id=mid, recipient='example_hermes_bot',
                                    text=f'msg b{mid}', timeout=10, now=100)[0]
            self.store.queued(r['id'])
            rows_b.append(r)

        channel = self.channel()
        channel._sender = None
        channel.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=9999)))

        # Chat B has 3 tickets (meets threshold 3), Chat A has 2 tickets (below threshold).
        # Digest must be formed only for Chat B, and sent to Chat B.
        run(channel.flush_replies())
        self.assertEqual(len(channel.bot.send_message.call_args_list), 1)
        call_kwargs = channel.bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs['chat_id'], chat_b)
        self.assertIn('3 reply tickets overdue', call_kwargs['text'])
        for r in rows_b:
            self.assertIn(r['id'][:8], call_kwargs['text'])
        # Tickets in Chat A must not be alerted or notified in this digest
        for r in (row_a1, row_a2):
            self.assertEqual(self.store.get(r['id'])['alerts'], 0)
        # Tickets in Chat B have alerts incremented
        for r in rows_b:
            self.assertEqual(self.store.get(r['id'])['alerts'], 1)

    def test_digest_alert_aborts_if_frozen_after_selection(self):
        """#51.N3: if transport freezes after overdue selection, digest alert emission is aborted cleanly."""
        rows = [self.ticket(mid=mid) for mid in (1, 2, 3)]
        channel = self.channel()
        original_select = channel._select_digest

        def freeze_and_select(overdue_rows):
            group = original_select(overdue_rows)
            channel._frozen = True  # freeze between selection and dispatch
            return group

        channel._select_digest = freeze_and_select
        run(channel.flush_replies())

        self.assertEqual(channel._test_sender.sent, [])
        for r in rows:
            self.assertEqual(self.store.get(r['id'])['alerts'], 0)


if __name__ == '__main__':
    unittest.main()


# Issue #106: ``AgentConfig.from_dict`` merges the developer's real ``.env`` into the
# process-global ``os.environ``; the guard keeps this module's tests from leaking it.
from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)
