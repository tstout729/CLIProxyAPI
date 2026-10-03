// CLI Proxy: a small Mac window around the proxy's management dashboard.
//
// View > This Mac shows the fallback proxy on 127.0.0.1:8318; View > M1 shows
// the main gateway through the Tailscale tunnel on 127.0.0.1:18318. Each
// dashboard remembers its own sign-in. Provider OAuth pages open in Safari.
import AppKit
import WebKit

struct Target {
    let name: String
    let url: URL
    let keyFile: String
    let downHint: String
}

let configDir = NSString(string: "~/.config/cliproxyapi-custom").expandingTildeInPath
let targets = [
    Target(name: "This Mac",
           url: URL(string: "http://127.0.0.1:8318/management.html")!,
           keyFile: configDir + "/management-key",
           downHint: "The local proxy is not running. In Terminal: launchctl kickstart -k gui/$(id -u)/io.tstout.cliproxyapi-custom"),
    Target(name: "M1",
           url: URL(string: "http://127.0.0.1:18318/management.html")!,
           keyFile: configDir + "/m1-management-key",
           downHint: "The M1 tunnel is down. Check that the M1 is awake and on Tailscale, then: launchctl kickstart -k gui/$(id -u)/io.tstout.cliproxy-m1-tunnel"),
]

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var current = UserDefaults.standard.integer(forKey: "target") % targets.count

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.contentView = webView
        window.setFrameAutosaveName("CLIProxyDashboard")
        window.makeKeyAndOrderFront(nil)
        show(current)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func show(_ index: Int) {
        current = index
        UserDefaults.standard.set(index, forKey: "target")
        window.title = "CLI Proxy — \(targets[index].name)"
        webView.load(URLRequest(url: targets[index].url))
    }

    @objc func showLocal() { show(0) }
    @objc func showM1() { show(1) }
    @objc func reload() { show(current) }

    @objc func copyKey() {
        guard let key = try? String(contentsOfFile: targets[current].keyFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines), !key.isEmpty else {
            NSSound.beep()
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(key, forType: .string)
    }

    @objc func openInSafari() { openExternally(targets[current].url) }

    func openExternally(_ url: URL) {
        let safari = URL(fileURLWithPath: "/Applications/Safari.app")
        NSWorkspace.shared.open([url], withApplicationAt: safari, configuration: NSWorkspace.OpenConfiguration())
    }

    // Links that leave the dashboard (provider sign-in pages) open in Safari.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let host = url.host, host != "127.0.0.1", host != "localhost",
           action.navigationType == .linkActivated {
            openExternally(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { openExternally(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let target = targets[current]
        let html = """
        <html><body style="font: 15px -apple-system; padding: 48px; color: #333">
        <h2>Can't reach the \(target.name) proxy</h2>
        <p>\(target.downHint)</p>
        <p>Then choose View &gt; Reload (⌘R).</p></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    func buildMenu() {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem()
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            item.submenu = menu
            main.addItem(item)
        }
        func item(_ title: String, _ action: Selector?, _ key: String, _ target: AnyObject? = nil) -> NSMenuItem {
            let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: key)
            menuItem.target = target
            return menuItem
        }
        submenu("CLI Proxy", [
            item("Hide CLI Proxy", #selector(NSApplication.hide(_:)), "h"),
            .separator(),
            item("Quit CLI Proxy", #selector(NSApplication.terminate(_:)), "q"),
        ])
        // Standard edit actions so the management key can be pasted into the dashboard.
        submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "Z"),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        submenu("View", [
            item("This Mac", #selector(showLocal), "1", self),
            item("M1", #selector(showM1), "2", self),
            .separator(),
            item("Reload", #selector(reload), "r", self),
            item("Copy Management Key", #selector(copyKey), "k", self),
            item("Open in Safari", #selector(openInSafari), "o", self),
        ])
        submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
