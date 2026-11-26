import asyncio
import base64
import unittest
from unittest.mock import Mock, AsyncMock, patch
import main
import gmail_tasks


class GmailTests(unittest.TestCase):
    def test_body_prefers_text_and_ignores_attachments(self):
        enc=lambda s: base64.urlsafe_b64encode(s.encode()).decode()
        self.assertEqual(gmail_tasks._body({'parts': [
            {'mimeType':'text/plain','body':{'data':enc('Please reply')}},
            {'mimeType':'text/html','body':{'data':enc('<b>Please reply</b>')}}]}), 'Please reply')
        self.assertEqual(gmail_tasks._body({'filename':'private.txt','mimeType':'text/plain','body':{'data':enc('attachment')}}), '')

    def test_unknown_sources_and_duplicate_actions_are_filtered(self):
        threads=[{'id':'abc123','messages':[{'subject':'Confirm appointment'}]}]
        task={'thread_id':'abc123','action':'Подтвердить запись','deadline':None}
        rendered='\n'.join(gmail_tasks.render_tasks([task,task,{'thread_id':'invented','action':'Не должно появиться'}],threads,True))
        self.assertEqual(rendered.count('• Подтвердить запись'),1)
        self.assertNotIn('Не должно появиться',rendered)
        self.assertIn('#all/abc123',rendered)
        self.assertIn('Не указан в письме',rendered)
        self.assertIn('50 последних',rendered)

    def test_chunks_fit_telegram(self):
        threads=[{'id':str(i),'messages':[{'subject':'X'*160}]} for i in range(12)]
        tasks=[{'thread_id':str(i),'action':'A'*600,'deadline':'D'*120} for i in range(12)]
        self.assertTrue(all(len(c)<4000 for c in gmail_tasks.render_tasks(tasks,threads)))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_school_digest_does_not_process_calendar_events(self):
        app=Mock();app.bot.send_message=AsyncMock()
        with patch.object(main.summarizer,'summarize',return_value=(['important'],[{'title':'event'}])), patch.object(main,'_save_events') as save:
            await main._send_messages_and_events(app,123,[{'id':'1'}])
            save.assert_not_called()
            self.assertEqual(app.bot.send_message.await_count,1)
            self.assertNotIn('reply_markup',app.bot.send_message.call_args.kwargs)

    async def test_scheduler_has_gmail_but_no_event_reminders(self):
        scheduler=Mock()
        with patch.object(main,'AsyncIOScheduler',return_value=scheduler):
            await main.post_init(Mock())
        funcs=[call.args[0] for call in scheduler.add_job.call_args_list]
        self.assertIn(main.send_gmail_actions,funcs)
        self.assertNotIn(main.send_reminders,funcs)

    async def test_unconfigured_gmail_is_not_reported_as_empty_inbox(self):
        app=Mock();app.bot.send_message=AsyncMock()
        with patch.object(main.gmail_tasks,'is_configured',return_value=False):
            await main.send_gmail_actions(app,123)
        self.assertIn('ещё не подключён',app.bot.send_message.call_args.kwargs['text'])

    async def test_gmail_error_is_not_reported_as_no_actions(self):
        app=Mock();app.bot.send_message=AsyncMock()
        with patch.object(main,'_gmail_lock',asyncio.Lock()), patch.object(main.gmail_tasks,'is_configured',return_value=True), patch.object(main.gmail_tasks,'daily_digest',side_effect=ValueError('invalid_grant')):
            await main.send_gmail_actions(app,123)
        self.assertIn('Не удалось',app.bot.send_message.call_args.kwargs['text'])

    async def test_old_calendar_command_does_not_write(self):
        update=Mock(); update.message.reply_text=AsyncMock()
        await main.cmd_calendar_disabled(update,Mock())
        self.assertIn('отключены',update.message.reply_text.call_args.args[0])

if __name__=='__main__':
    unittest.main()
