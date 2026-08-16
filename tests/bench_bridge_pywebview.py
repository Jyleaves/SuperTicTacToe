"""桥延迟对比：pywebview(JS→Python→ctypes→Rust) 的 ping 往返耗时。
运行：python tests/bench_bridge_pywebview.py（会短暂弹出窗口约 3 秒）
对照：纯 Rust 窗口的基准为 `sttt-app.exe "?bench"`。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HTML = """
<html><body>
<script>
window.__benchResult = null;
function tryRun(attempt) {
  if (window.__benchResult !== null) return;
  var api = window.pywebview && window.pywebview.api;
  if (!api) {                       // 桥未注入：稍后重试（pywebview 页面加载后才注入）
    if (attempt < 200) setTimeout(function () { tryRun(attempt + 1); }, 50);
    else window.__benchResult = -1;
    return;
  }
  api.ping().then(function () {
    var t0 = performance.now();
    var N = 500;
    var chain = Promise.resolve();
    for (var i = 0; i < N; i++) chain = chain.then(function () { return api.ping(); });
    chain.then(function () {
      window.__benchResult = (performance.now() - t0) / N;
    });
  }).catch(function (e) { window.__benchResult = -2; });
}
window.addEventListener('load', function () { tryRun(0); });
</script>
</body></html>
"""


def main():
    import webview
    from super_ttt.server import Api

    api = Api()
    window = webview.create_window("bench", html=HTML, js_api=api,
                                   width=300, height=200)

    def poll(_window=None):
        window.events.loaded.wait()          # 关键：加载完成前 evaluate_js 会死锁
        for _ in range(200):
            try:
                r = window.evaluate_js("window.__benchResult")
            except Exception:
                r = None
            if r is not None:
                print(f"[bench] pywebview 桥往返延迟: {float(r):.3f} ms/次 (500 次 ping)")
                window.destroy()
                return
            time.sleep(0.05)
        print("未取到结果")
        window.destroy()

    webview.start(poll, window, gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
