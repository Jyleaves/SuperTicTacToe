//! SuperTicTacToe 纯 Rust 桌面应用（wry/tao，Windows 上同为 WebView2 内核）。
//!
//! 与旧架构（pywebview：JS → .NET/COM 桥 → Python 线程 → ctypes → Rust DLL）相比，
//! 本应用为 JS → window.ipc → Rust 回调 → evaluate_script，
//! 全程同机直连无中间层：调用往返亚毫秒，启动不再拉起 Python。
//!
//! 前端 web/ 目录零改动：初始化脚本注入 window.pywebview.api 兼容垫片，
//! app.js 的桥代理无感切换。界面/样式/动画完全一致。
//!
//! 发布形态：GUI 子系统（双击无控制台黑框）+ 静态链接 CRT
//! （见 rust/.cargo/config.toml 的 +crt-static，单文件免 VC++ 运行库）。

// GUI 子系统：双击不弹控制台（debug 构建保留控制台便于开发）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::cell::RefCell;
use std::io::Write;
use std::rc::Rc;

use serde_json::Value;
use tao::{
    event::{Event, StartCause, WindowEvent},
    event_loop::{ControlFlow, EventLoopProxy},
    window::WindowBuilder,
};
use wry::{WebView, WebViewBuilder, WebViewBuilderExtWindows};

// 前端资源内嵌进二进制：单文件分发，无运行时文件依赖
const INDEX_HTML: &str = include_str!("../../../web/index.html");
const APP_JS: &str = include_str!("../../../web/app.js");
const MOCK_JS: &str = include_str!("../../../web/mock.js");
const STYLE_CSS: &str = include_str!("../../../web/style.css");

enum UserEvent {
    /// 快方法（ping/play/stats 等，微秒级）：直接在 UI 线程分发
    Rpc { id: u64, method: String, args: Vec<Value> },
    /// worker 线程完成长操作（ai_move 搜索）：回传结果
    Respond { id: u64, ok: bool, json: String },
    /// 页面 DOM 就绪（或兜底超时）：显示窗口，消除初始化白屏
    Show,
    /// JS 调 exit_app / 基准测试完成：退出事件循环
    Exit,
}

/// WebView 在 Windows 上非 Send/Sync：只能事件循环线程持有，
/// 用 Rc<RefCell<Option<_>>> 存进 run 闭包（ipc 回调经 UserEvent 回主线程）
type SharedWebView = Rc<RefCell<Option<WebView>>>;

/// 注入的桥垫片：把 window.pywebview.api.* 映射到 ipc 请求/响应 Promise。
/// 另含 ?bench 模式：测 IPC 往返延迟并回报（供迁移对比，与游戏无关）。
const ADAPTER_JS: &str = r#"
(function () {
  if (window.pywebview) return;
  var nextId = 1;
  var pending = new Map();
  window.__stttIpcResult = function (id, ok, json) {
    var p = pending.get(id);
    if (!p) return;
    pending.delete(id);
    if (ok) { try { p.res(JSON.parse(json)); } catch (e) { p.rej(e); } }
    else { p.rej(new Error(json)); }
  };
  var METHODS = ['ping', 'precompile_status', 'new_game', 'play', 'ai_move',
                 'resign', 'stats', 'legal_moves', 'exit_app'];
  var api = {};
  METHODS.forEach(function (m) {
    api[m] = function () {
      var args = Array.prototype.slice.call(arguments);
      return new Promise(function (res, rej) {
        var id = nextId++;
        pending.set(id, { res: res, rej: rej });
        window.ipc.postMessage(JSON.stringify({ id: id, method: m, args: args }));
      });
    };
  });
  window.pywebview = { api: api };

  // 页面就绪信号：DOMContentLoaded 即上报（隐藏窗口下 rAF/timer 可能被
  // 节流，DOMContentLoaded 不受影响），宿主收到后显示窗口——内容就位
  // 才露面，消除 WebView2 初始化期的白屏
  window.addEventListener('DOMContentLoaded', function () {
    window.ipc.postMessage(JSON.stringify({ method: '__page_ready', args: [] }));
  });

  // ?bench：测 500 次 ping 的 IPC 平均往返延迟，结果回报给宿主
  window.addEventListener('load', function () {
    if (!/[?&]bench\b/.test(location.search)) return;
    setTimeout(function () {
      Promise.resolve()
        .then(function () { return api.ping(); })
        .then(function () {
          var t0 = performance.now();
          var N = 500;
          var chain = Promise.resolve();
          for (var i = 0; i < N; i++) {
            chain = chain.then(function () { return api.ping(); });
          }
          return chain.then(function () { return (performance.now() - t0) / N; });
        })
        .then(function (avgMs) {
          window.ipc.postMessage(JSON.stringify({ method: '__bench_report', args: [avgMs] }));
        })
        .catch(function (e) {
          window.ipc.postMessage(JSON.stringify({ method: '__bench_report', args: [-1] }));
        });
    }, 120);
  });
})();
"#;

fn serve(path: &str) -> wry::http::Response<std::borrow::Cow<'static, [u8]>> {
    use std::borrow::Cow;
    let (body, mime): (&str, &str) = match path {
        "/" | "/index.html" => (INDEX_HTML, "text/html; charset=utf-8"),
        "/app.js" => (APP_JS, "text/javascript; charset=utf-8"),
        "/mock.js" => (MOCK_JS, "text/javascript; charset=utf-8"),
        "/style.css" => (STYLE_CSS, "text/css; charset=utf-8"),
        _ => ("not found", "text/plain; charset=utf-8"),
    };
    let status = if body == "not found" { 404 } else { 200 };
    wry::http::Response::builder()
        .header(wry::http::header::CONTENT_TYPE, mime)
        .status(status)
        .body(Cow::Borrowed(body.as_bytes()))
        .unwrap()
}

/// JS 字符串字面量转义（JSON 嵌入 evaluate_script 用）
fn js_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn respond(wv: &SharedWebView, id: u64, ok: bool, json: String) {
    let js = format!(
        "window.__stttIpcResult({},{},\"{}\")",
        id, ok, js_escape(&json)
    );
    if let Some(webview) = wv.borrow().as_ref() {
        let _ = webview.evaluate_script(&js);
    }
}

/// RPC 分发（worker 线程执行，长搜索不阻塞 UI 线程）
fn dispatch(method: &str, args: &[Value]) -> Result<String, String> {
    match method {
        "ping" => Ok(sttt::session::ping_json()),
        "precompile_status" => Ok(sttt::session::precompile_json()),
        "stats" => Ok(sttt::session::stats_json()),
        "legal_moves" => Ok(sttt::session::legal_moves_json()),
        "new_game" => {
            let s = args.first().ok_or("new_game: missing settings")?;
            let g = |k: &str| s.get(k).and_then(Value::as_i64).unwrap_or(0) as i32;
            sttt::session::new_game(g("mode"), g("difficulty"), g("first"), g("goal"));
            Ok(sttt::session::state_json())
        }
        "play" => {
            let sub = args.first().and_then(Value::as_i64).unwrap_or(0) as i32;
            let cell = args.get(1).and_then(Value::as_i64).unwrap_or(0) as i32;
            sttt::session::play(sub, cell);
            Ok(sttt::session::state_json())
        }
        "ai_move" => {
            sttt::session::ai_move();
            Ok(sttt::session::state_json())
        }
        "resign" => {
            sttt::session::resign();
            Ok(sttt::session::state_json())
        }
        _ => Err(format!("unknown method: {method}")),
    }
}

/// GUI 子系统下无控制台：致命错误/panic 用原生 MessageBox 呈现，
/// 否则用户只会看到无声闪退（零依赖：直接声明 user32 导入）。
#[cfg(windows)]
mod native_msg {
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(hwnd: isize, text: *const u16, caption: *const u16, utype: u32) -> i32;
    }

    pub fn error(text: &str) {
        use std::os::windows::ffi::OsStrExt;
        let t: Vec<u16> = std::ffi::OsStr::new(text)
            .encode_wide()
            .chain(Some(0))
            .collect();
        let c: Vec<u16> = std::ffi::OsStr::new("超级井字棋")
            .encode_wide()
            .chain(Some(0))
            .collect();
        const MB_ICONERROR: u32 = 0x10;
        unsafe { MessageBoxW(0, t.as_ptr(), c.as_ptr(), MB_ICONERROR) };
    }
}

#[cfg(not(windows))]
mod native_msg {
    pub fn error(text: &str) {
        eprintln!("{text}");
    }
}

fn main() {
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::time::Duration;

    let t0 = std::time::Instant::now();
    // 启动分阶段计时（?startup 模式落盘）
    static T_BUILT: AtomicU64 = AtomicU64::new(0);
    static T_READY: AtomicU64 = AtomicU64::new(0);
    static T_SHOWN: AtomicU64 = AtomicU64::new(0);
    static SHOWN: AtomicBool = AtomicBool::new(false);
    let startup_mode = std::env::args().any(|a| a.starts_with("?startup"));

    // panic 无声闪退 → 弹窗（拷贝分发时用户至少知道发生了什么）
    std::panic::set_hook(Box::new(|info| {
        native_msg::error(&format!("程序发生内部错误：\n{info}"));
    }));

    let event_loop = tao::event_loop::EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy: EventLoopProxy<UserEvent> = event_loop.create_proxy();

    // 隐藏创建：WebView2 初始化（进程拉起 + 环境建立 + 首帧）需要数百毫秒，
    // 期间窗口露浅色底。就绪信号到达（或 3s 兜底）才显示——内容就位即呈现
    let window = match WindowBuilder::new()
        .with_title("超级井字棋")
        .with_inner_size(tao::dpi::LogicalSize::new(660.0, 740.0))
        .with_min_inner_size(tao::dpi::LogicalSize::new(560.0, 640.0))
        .with_visible(false)
        .build(&event_loop)
    {
        Ok(w) => w,
        Err(e) => {
            native_msg::error(&format!("创建窗口失败：\n{e}"));
            return;
        }
    };

    // 支持 sttt-app.exe "?bench"/"?startup" 或完整 URL 覆盖（测试/基准用）
    let mut url = "sttt://localhost/index.html".to_string();
    for arg in std::env::args().skip(1) {
        if arg.starts_with("sttt://") {
            url = arg;
        } else if arg.starts_with('?') {
            url.push_str(&arg);
        }
    }

    let ipc_proxy = proxy.clone();
    let webview = WebViewBuilder::new()
        .with_custom_protocol("sttt".into(), move |_id, request| {
            serve(request.uri().path())
        })
        // 启动提速：在 wry 默认禁用项之上再关掉后台网络/组件更新/扩展等
        // 与渲染无关的浏览器服务（实测对比见 PROGRESS.md）
        .with_additional_browser_args(
            "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection,\
             AutoSuggestInPostBack,TypeRetirementFlight,AutofillServerCommunication \
             --disable-extensions --disable-sync --disable-background-networking \
             --disable-component-update --renderer-process-limit=1 --noerrdialogs",
        )
        .with_initialization_script(ADAPTER_JS)
        .with_ipc_handler(move |request| {
            let msg = request.body().to_string();
            let v: Value = match serde_json::from_str(&msg) {
                Ok(v) => v,
                Err(_) => return,
            };
            let id = v.get("id").and_then(Value::as_u64).unwrap_or(0);
            let method = v
                .get("method")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let args: Vec<Value> = v
                .get("args")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();

            // 页面就绪：显示窗口（不回传数据）
            if method == "__page_ready" {
                T_READY.store(t0.elapsed().as_millis() as u64, Ordering::Relaxed);
                let _ = ipc_proxy.send_event(UserEvent::Show);
                return;
            }

            // 这两个方法不回传数据，只触发退出（页面即将销毁）
            if method == "exit_app" || method == "__bench_report" {
                if method == "__bench_report" {
                    let avg = args.first().and_then(Value::as_f64).unwrap_or(-1.0);
                    let line = format!("[bench] IPC round-trip latency: {avg:.3} ms (500 pings)\n");
                    // GUI 子系统无控制台：stdout 写失败忽略，结果同时落盘供脚本读取
                    let _ = std::io::stdout().write_all(line.as_bytes());
                    let _ = std::fs::write("bench_result.txt", &line);
                }
                let _ = ipc_proxy.send_event(UserEvent::Exit);
                return;
            }

            // ai_move 含 MCTS 搜索（最长 ~秒级）→ 工作线程，UI 动画不冻结；
            // 其余方法微秒级 → 直接派回 UI 线程执行（省线程创建开销）
            if method == "ai_move" {
                let p = ipc_proxy.clone();
                std::thread::spawn(move || match dispatch(&method, &args) {
                    Ok(json) => {
                        let _ = p.send_event(UserEvent::Respond { id, ok: true, json });
                    }
                    Err(e) => {
                        let _ = p.send_event(UserEvent::Respond { id, ok: false, json: e });
                    }
                });
            } else {
                let _ = ipc_proxy.send_event(UserEvent::Rpc { id, method, args });
            }
        })
        .with_url(url)
        .build(&window);

    let webview = match webview {
        Ok(wv) => wv,
        Err(e) => {
            // 最常见原因：目标机器缺少 WebView2 运行时
            native_msg::error(&format!(
                "初始化界面失败：\n{e}\n\n最常见原因：缺少 Microsoft Edge WebView2 运行时。\n\
                 Windows 11 与已更新的 Windows 10 通常自带；否则请安装：\n\
                 https://developer.microsoft.com/microsoft-edge/webview2/"
            ));
            return;
        }
    };

    let shared: SharedWebView = Rc::new(RefCell::new(Some(webview)));
    T_BUILT.store(t0.elapsed().as_millis() as u64, Ordering::Relaxed);

    let show_deadline = t0 + Duration::from_secs(3);   // 就绪信号丢失时的兜底
    event_loop.run(move |event, _, control_flow| {
        // 未显示前用 WaitUntil 兜底：3s 内没等到 __page_ready 也强制显示
        if !SHOWN.load(Ordering::Relaxed) {
            *control_flow = ControlFlow::WaitUntil(show_deadline);
        } else {
            *control_flow = ControlFlow::Wait;
        }
        match event {
            Event::UserEvent(UserEvent::Show) => {
                if !SHOWN.swap(true, Ordering::Relaxed) {
                    T_SHOWN.store(t0.elapsed().as_millis() as u64,
                                  Ordering::Relaxed);
                    let _ = window.set_visible(true);
                    if startup_mode {
                        let report = format!(
                            "[startup] webview构建 {}ms | DOM就绪 {}ms | 显示 {}ms\n",
                            T_BUILT.load(Ordering::Relaxed),
                            T_READY.load(Ordering::Relaxed),
                            T_SHOWN.load(Ordering::Relaxed));
                        let _ = std::io::Write::write_all(
                            &mut std::io::stdout(), report.as_bytes());
                        let _ = std::fs::write("startup_result.txt", &report);
                        let _ = proxy.send_event(UserEvent::Exit);
                    }
                }
            }
            Event::NewEvents(StartCause::ResumeTimeReached { .. })
                if !SHOWN.load(Ordering::Relaxed) =>
            {
                // 兜底超时：强制显示
                let _ = proxy.send_event(UserEvent::Show);
            }
            Event::UserEvent(UserEvent::Rpc { id, method, args }) => {
                match dispatch(&method, &args) {
                    Ok(json) => respond(&shared, id, true, json),
                    Err(e) => respond(&shared, id, false, e),
                }
            }
            Event::UserEvent(UserEvent::Respond { id, ok, json }) => {
                respond(&shared, id, ok, json)
            }
            Event::UserEvent(UserEvent::Exit) => *control_flow = ControlFlow::Exit,
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => *control_flow = ControlFlow::Exit,
            Event::WindowEvent {
                event: WindowEvent::Destroyed,
                ..
            } => *control_flow = ControlFlow::Exit,
            _ => {}
        }
    });
}
