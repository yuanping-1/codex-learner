"""Static Chromium checks with an explicit host mock, not native-host acceptance."""

import json
import os
import re
import shutil
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import CourseCase, ROOT
from codex_learner.panel import render
from playwright.sync_api import sync_playwright

MOCK = """
window.sentRequests = [];
window.cachedStates = [];
window.openai = {
  widgetState: null,
  setWidgetState: async state => { window.cachedStates.push(state); },
  sendFollowUpMessage: async request => { window.sentRequests.push(request); return {}; }
};
"""


class BrowserTests(CourseCase):
    def setUp(self):
        super().setUp()
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(self.root)))
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        executable = os.environ.get("LEARNER_CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser")
        cls.browser = cls.playwright.chromium.launch(executable_path=executable, headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_panel(self, mock=MOCK, state=None, width=736):
        state = state or self.library.load("sample-course")
        path = self.root / (f"panel-{len(list(self.root.glob('panel-*.html')))}.html")
        render(state, path)
        context = self.browser.new_context(viewport={"width": width, "height": 900}, timezone_id="Asia/Shanghai")
        self.addCleanup(context.close)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        if mock:
            page.add_init_script(mock)
        page.goto("http://127.0.0.1:" + str(self.server.server_port) + "/" + path.name)
        self.assertEqual(errors, [])
        return page, path

    def captured_request(self, page, index=-1):
        payload = page.evaluate("window.sentRequests")[index]["prompt"]
        return json.loads(re.search(r"<learner-request>\n(.*)\n</learner-request>", payload, re.S).group(1))

    def test_e01_browser_to_real_file_roundtrip_and_updated_receipt(self):
        page, _ = self.open_panel()
        answer = '中文、多行\n"quotes" \\ path\n$(not-a-command)\n</script><script>throw 1</script>'
        page.get_by_label("你的回答").fill(answer)
        page.get_by_role("button", name="提交答案到聊天").click()
        request = self.captured_request(page)
        self.assertEqual(request["answer"], answer)
        self.assertIn("尚未确认", page.locator("#draft-status").inner_text())
        receipt = self.engine.apply(request)
        self.assertEqual(receipt["result"]["answer"], answer)
        page.get_by_role("button", name="提交答案到聊天").click()
        self.assertEqual(self.captured_request(page), request)
        self.assertTrue(self.engine.apply(self.captured_request(page))["duplicate"])
        page.get_by_label("题目", exact=True).select_option(index=1)
        page.get_by_label("你的回答").fill("包含停留时间")
        page.get_by_role("button", name="提交答案到聊天").click()
        self.engine.apply(self.captured_request(page))
        updated, _ = self.open_panel()
        self.assertIn("答案已记录", updated.locator("#receipt").inner_text())
        self.assertEqual(len(self.library.load("sample-course")["submissions"]), 2)

    def test_e02_cancellation_retains_full_draft_without_saving(self):
        page, _ = self.open_panel(MOCK + "\nwindow.openai.sendFollowUpMessage = async () => ({cancelled:true});")
        page.locator("#answer").fill("取消后保留")
        page.locator("#submit-answer").click()
        self.assertEqual(page.locator("#answer").input_value(), "取消后保留")
        self.assertIn("已取消", page.locator("#draft-status").inner_text())
        self.assertEqual(self.library.load("sample-course")["submissions"], {})

    def test_e02_queued_request_can_retry_after_removal_without_host_callback(self):
        page, _ = self.open_panel(MOCK + """
window.pendingRequests = [];
window.openai.sendFollowUpMessage = async request => {
  window.sentRequests.push(request);
  window.pendingRequests.push(request);
  return {};
};
""")
        before = self.library.load("sample-course")
        answer = '排队后取消\n"原样重试"'
        page.locator("#answer").fill(answer)
        page.locator("#submit-answer").click()
        request = self.captured_request(page)
        self.assertEqual(page.evaluate("window.pendingRequests.length"), 1)
        page.evaluate("window.pendingRequests.splice(0)")
        self.assertEqual(page.locator("#answer").input_value(), answer)
        status = page.locator("#draft-status").inner_text()
        self.assertIn("删除排队消息", status)
        self.assertIn("重试", status)
        self.assertNotIn("等待处理", status)
        self.assertNotIn("请求已取消", status)
        page.locator("#submit-answer").click()
        self.assertEqual(self.captured_request(page), request)
        page.evaluate("window.pendingRequests.splice(0)")
        self.assertEqual(self.library.load("sample-course"), before)

    def test_e02_host_failure_offers_complete_recovery_request(self):
        page, _ = self.open_panel(MOCK + "\nwindow.openai.sendFollowUpMessage = async () => { throw Error('offline'); };")
        page.locator("#answer").fill("完整原答\n第二行")
        page.locator("#submit-answer").click()
        recovery = page.locator("#recovery-text").input_value()
        self.assertIn("完整原答\\n第二行", recovery)
        self.assertTrue(page.locator("#recovery").is_visible())
        self.assertEqual(page.locator("#answer").input_value(), "完整原答\n第二行")

    def test_no_host_is_labeled_and_not_reported_as_saved(self):
        page, _ = self.open_panel(mock=None)
        self.assertIn("静态面板", page.locator("#host-status").inner_text())
        page.locator("#answer").fill("test")
        page.locator("#submit-answer").click()
        self.assertIn("发送未完成", page.locator("#draft-status").inner_text())

    def test_no_reference_answers_or_executable_user_markup_in_initial_panel(self):
        state = self.library.load("sample-course")
        state["title"] = '</script><script>window.injected = true</script>{{SCRIPT}}'
        state["questions"]["q1"]["versions"]["qv1"]["prompt"] = '<img src=x onerror="window.injected=true">literal'
        page, path = self.open_panel(state=state)
        self.assertFalse(page.evaluate("Boolean(window.injected)"))
        self.assertIn("{{SCRIPT}}", page.locator("#course-title").inner_text())
        self.assertEqual(page.locator("#question-prompt img").count(), 0)
        html = path.read_text()
        self.assertNotIn("reference_answer", html)
        self.assertNotIn("5 米每秒，因为总路程除以总时间。", html)

    def test_e19_large_draft_is_not_truncated_and_cache_budget_is_respected(self):
        page, _ = self.open_panel()
        self.assertEqual(page.evaluate("window.cachedStates.length"), 0)
        answer = "中文长答案" * 5000
        page.locator("#answer").fill(answer)
        self.assertIn("超过缓存容量", page.locator("#cache-status").inner_text())
        page.locator("#submit-answer").click()
        self.assertEqual(self.captured_request(page)["answer"], answer)
        self.assertTrue(all(len(json.dumps(s, ensure_ascii=False).encode()) < 16384
                            for s in page.evaluate("window.cachedStates")))
        for answer in ("缩短后的草稿", ""):
            page.locator("#answer").fill(answer)
            self.assertEqual(page.locator("#cache-status").inner_text(), "")
            cached = page.evaluate("window.cachedStates.at(-1).privateContent")
            self.assertEqual(cached["drafts"][cached["selected"]]["answer"], answer)

    def test_cache_failure_from_old_draft_does_not_replace_current_status(self):
        page, _ = self.open_panel(MOCK + """
window.cacheWrites = [];
window.openai.setWidgetState = state => new Promise((resolve, reject) => {
  window.cacheWrites.push({state, resolve, reject});
});
""")
        page.locator("#answer").fill("较早的草稿")
        page.locator("#answer").fill("中文长答案" * 5000)
        page.evaluate("window.cacheWrites[0].reject(Error('old failure'))")
        self.assertIn("超过缓存容量", page.locator("#cache-status").inner_text())
        page.locator("#answer").fill("缓存失败后修改")
        page.evaluate("window.cacheWrites[1].reject(Error('current failure'))")
        self.assertIn("缓存失败", page.locator("#cache-status").inner_text())
        page.locator("#answer").fill("仍在缓存的草稿")
        page.locator("#answer").fill("")
        page.evaluate("""() => {
          window.cacheWrites[3].resolve();
          window.cacheWrites[2].reject(Error('late failure'));
        }""")
        self.assertEqual(page.locator("#cache-status").inner_text(), "")
        self.assertEqual(page.locator("#answer").input_value(), "")

    def test_cache_events_do_not_overwrite_active_edit_or_save_on_restore(self):
        page, _ = self.open_panel()
        page.locator("#answer").fill("当前正在编辑")
        before = page.evaluate("window.cachedStates.length")
        page.evaluate("""() => window.dispatchEvent(new CustomEvent('openai:set_globals',
          {detail:{globals:{widgetState:{privateContent:{course_id:'wrong',drafts:{}}}}}}))""")
        self.assertEqual(page.locator("#answer").input_value(), "当前正在编辑")
        self.assertEqual(page.evaluate("window.cachedStates.length"), before)

    def test_e19_narrow_wide_theme_and_keyboard(self):
        for width in (320, 736):
            for scheme in ("light", "dark"):
                with self.subTest(width=width, scheme=scheme):
                    page, _ = self.open_panel(width=width)
                    page.emulate_media(color_scheme=scheme)
                    page.evaluate("document.body.style.fontSize='200%'")
                    self.assertTrue(page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"))
                    page.locator("#answer").focus()
                    page.keyboard.type("keyboard answer")
                    page.keyboard.press("Tab")
                    self.assertEqual(page.evaluate("document.activeElement.id"), "submit-answer")
                    page.keyboard.press("Enter")
                    self.assertEqual(self.captured_request(page)["answer"], "keyboard answer")
        screenshot_page, _ = self.open_panel(width=736)
        output = ROOT / "artifacts"
        output.mkdir(exist_ok=True)
        screenshot_page.screenshot(path=str(output / "panel-static.png"), full_page=True)

    def test_snapshot_output_refuses_to_overwrite_historical_panel(self):
        page, path = self.open_panel()
        from codex_learner.common import LearnerError
        with self.assertRaises(LearnerError):
            render(self.library.load("sample-course"), path)
