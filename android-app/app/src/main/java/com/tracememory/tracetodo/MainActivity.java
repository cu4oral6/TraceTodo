package com.tracememory.tracetodo;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.MediaStore;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {

    private static final String PREFS_NAME = "tracetodo_native_prefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_USER_CUSTOMIZED = "server_user_customized";
    private static final String KEY_LAST_THEME_BG = "last_theme_bg";
    private static final String KEY_AUTH_TOKEN = "auth_token";

    private static final int REQ_FILE_CHOOSER = 1001;
    private static final int REQ_WEB_PERMISSIONS = 1002;
    private static final int REQ_POST_NOTIF = 1003;
    private static final int REQ_CAMERA_CAPTURE = 1004;
    private static final int REQ_CAMERA_CAPTURE_PERM = 1005;

    private FrameLayout rootLayout;
    private WebView webView;
    private ProgressBar progressBar;
    private LinearLayout offlineOverlay;
    private TextView offlineDetailText;

    private ValueCallback<Uri[]> fileUploadCallback;
    private PermissionRequest pendingWebPermissionRequest;
    private File pendingCameraFile = null;
    private Uri pendingCameraUri = null;

    private String currentBaseUrl = "https://todo.531party.cn";
    private boolean triedFallbackOnColdLoad = false;
    private boolean pageLoadedOnce = false;
    private String pendingDeepAction = null;
    private String pendingSharedText = null;
    private long lastBackPressMs = 0L;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        try {
            int initialBg = Color.parseColor("#F4F6FB");
            SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            try {
                initialBg = sp.getInt(KEY_LAST_THEME_BG, initialBg);
            } catch (Throwable ignored) {
            }

            String defaultUrl = getString(R.string.default_server_url);
            boolean customized = false;
            String savedUrl = "";
            try {
                customized = sp.getBoolean(KEY_USER_CUSTOMIZED, false);
                savedUrl = sp.getString(KEY_SERVER_URL, "");
            } catch (Throwable ignored) {
            }

            if (!customized || savedUrl == null || savedUrl.trim().isEmpty()) {
                currentBaseUrl = defaultUrl;
                sp.edit().putString(KEY_SERVER_URL, defaultUrl).apply();
            } else {
                currentBaseUrl = cleanServerUrl(savedUrl);
            }

            captureIncomingIntent(getIntent());
            buildRootLayout(initialBg);
            applySystemBarColorsInt(initialBg);
            configureWebView();
            requestNotificationPermissionIfNeeded();
            loadServerHome(false);
        } catch (Throwable t) {
            showFatalFallbackView(t);
        }
    }

    private void showFatalFallbackView(Throwable t) {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.parseColor("#F4F6FB"));

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_HORIZONTAL);
        box.setPadding(dp(24), dp(48), dp(24), dp(32));

        TextView title = new TextView(this);
        title.setText("有迹 启动兼容提示");
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setTextColor(Color.parseColor("#0F172A"));
        box.addView(title);

        TextView sub = new TextView(this);
        sub.setText("当前设备系统 WebView 组件受限或正在升级，可直接使用系统浏览器打开工作台：");
        sub.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13.5f);
        sub.setTextColor(Color.parseColor("#475569"));
        LinearLayout.LayoutParams subLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        subLp.topMargin = dp(12);
        box.addView(sub, subLp);

        Button openBrowserBtn = new Button(this);
        openBrowserBtn.setText("🌐 使用系统浏览器打开 有迹");
        openBrowserBtn.setAllCaps(false);
        openBrowserBtn.setTextColor(Color.WHITE);
        GradientDrawable btnBg = new GradientDrawable();
        btnBg.setColor(Color.parseColor("#2563EB"));
        btnBg.setCornerRadius(dp(999));
        openBrowserBtn.setBackground(btnBg);
        openBrowserBtn.setPadding(dp(24), dp(12), dp(24), dp(12));
        openBrowserBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(currentBaseUrl + "/console"));
                    startActivity(i);
                } catch (Throwable ignored) {
                }
            }
        });
        LinearLayout.LayoutParams btnLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        btnLp.topMargin = dp(20);
        box.addView(openBrowserBtn, btnLp);

        StringWriter sw = new StringWriter();
        t.printStackTrace(new PrintWriter(sw));
        TextView trace = new TextView(this);
        trace.setText(sw.toString());
        trace.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        trace.setTextColor(Color.parseColor("#64748B"));
        LinearLayout.LayoutParams trLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        trLp.topMargin = dp(20);
        box.addView(trace, trLp);

        scroll.addView(box);
        setContentView(scroll);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        captureIncomingIntent(intent);
        if (pageLoadedOnce && webView != null) {
            dispatchPendingIntentActions();
        }
    }

    private void captureIncomingIntent(Intent intent) {
        if (intent == null) return;
        try {
            String action = intent.getAction();
            if (Intent.ACTION_SEND.equals(action) && "text/plain".equals(intent.getType())) {
                String shared = intent.getStringExtra(Intent.EXTRA_TEXT);
                if (shared != null && !shared.trim().isEmpty()) {
                    pendingSharedText = shared.trim();
                }
            } else if (Intent.ACTION_VIEW.equals(action) && intent.getData() != null) {
                Uri data = intent.getData();
                if ("tracetodo".equalsIgnoreCase(data.getScheme())) {
                    pendingDeepAction = data.getHost() != null ? data.getHost().toLowerCase() : "";
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private void buildRootLayout(int initialBg) {
        rootLayout = new FrameLayout(this);
        rootLayout.setBackgroundColor(initialBg);

        webView = new WebView(this);
        webView.setBackgroundColor(initialBg);
        rootLayout.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setVisibility(View.GONE);
        FrameLayout.LayoutParams pbParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(3)
        );
        pbParams.gravity = Gravity.TOP;
        rootLayout.addView(progressBar, pbParams);

        offlineOverlay = buildOfflineOverlay();
        offlineOverlay.setVisibility(View.GONE);
        rootLayout.addView(offlineOverlay, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        setContentView(rootLayout);
    }

    private LinearLayout buildOfflineOverlay() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setPadding(dp(28), dp(28), dp(28), dp(28));
        box.setBackgroundColor(Color.parseColor("#F4F6FB"));

        TextView icon = new TextView(this);
        icon.setText("☁️");
        icon.setTextSize(TypedValue.COMPLEX_UNIT_SP, 42);
        icon.setGravity(Gravity.CENTER);
        box.addView(icon);

        TextView title = new TextView(this);
        title.setText("无法连接到 有迹 云服务");
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setTextColor(Color.parseColor("#0F172A"));
        title.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams titleLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        titleLp.topMargin = dp(12);
        box.addView(title, titleLp);

        offlineDetailText = new TextView(this);
        offlineDetailText.setText("请检查移动网络或 Wi-Fi 连接后重试");
        offlineDetailText.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        offlineDetailText.setTextColor(Color.parseColor("#64748B"));
        offlineDetailText.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams subLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        subLp.topMargin = dp(8);
        box.addView(offlineDetailText, subLp);

        Button retryBtn = new Button(this);
        retryBtn.setText("立即重连");
        retryBtn.setTextColor(Color.WHITE);
        retryBtn.setAllCaps(false);
        GradientDrawable primaryBg = new GradientDrawable();
        primaryBg.setColor(Color.parseColor("#2563EB"));
        primaryBg.setCornerRadius(dp(999));
        retryBtn.setBackground(primaryBg);
        retryBtn.setPadding(dp(26), dp(10), dp(26), dp(10));
        retryBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                offlineOverlay.setVisibility(View.GONE);
                loadServerHome(true);
            }
        });
        LinearLayout.LayoutParams retryLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        retryLp.topMargin = dp(22);
        box.addView(retryBtn, retryLp);

        Button switchBtn = new Button(this);
        switchBtn.setText("切换服务端地址");
        switchBtn.setTextColor(Color.parseColor("#334155"));
        switchBtn.setAllCaps(false);
        GradientDrawable secBg = new GradientDrawable();
        secBg.setColor(Color.WHITE);
        secBg.setStroke(dp(1), Color.parseColor("#CBD5E1"));
        secBg.setCornerRadius(dp(999));
        switchBtn.setBackground(secBg);
        switchBtn.setPadding(dp(22), dp(8), dp(22), dp(8));
        switchBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showServerSwitchDialog();
            }
        });
        LinearLayout.LayoutParams swLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        swLp.topMargin = dp(10);
        box.addView(switchBtn, swLp);

        return box;
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private void configureWebView() {
        try {
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            cm.setAcceptThirdPartyCookies(webView, true);
        } catch (Throwable ignored) {
        }

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setAllowFileAccess(true);
        ws.setAllowContentAccess(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setBuiltInZoomControls(false);
        ws.setDisplayZoomControls(false);
        ws.setTextZoom(100);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        String origUa = ws.getUserAgentString();
        ws.setUserAgentString((origUa != null ? origUa : "") + " TraceTodoAndroid/1.4.8");

        webView.addJavascriptInterface(new NativeBridge(), "TraceTodoNative");

        webView.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimetype, long contentLength) {
                try {
                    if (url != null && (url.startsWith("http://") || url.startsWith("https://"))) {
                        Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
                        startActivity(i);
                    }
                } catch (Throwable ignored) {
                }
            }
        });

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (request == null) return false;
                Uri uri = request.getUrl();
                if (uri == null) return false;
                String scheme = uri.getScheme() != null ? uri.getScheme().toLowerCase() : "";
                if ("http".equals(scheme) || "https".equals(scheme)) {
                    String targetHost = uri.getHost() != null ? uri.getHost() : "";
                    Uri baseUri = Uri.parse(currentBaseUrl);
                    String baseHost = baseUri.getHost() != null ? baseUri.getHost() : "";
                    if (targetHost.equalsIgnoreCase(baseHost) || "43.228.79.133".equals(targetHost) || "todo.531party.cn".equalsIgnoreCase(targetHost)) {
                        return false;
                    }
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, uri));
                    } catch (Throwable ignored) {
                    }
                    return true;
                }
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                if (offlineOverlay != null) {
                    offlineOverlay.setVisibility(View.GONE);
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                pageLoadedOnce = true;
                injectNativeEnhancementBridge();
                dispatchPendingIntentActions();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                super.onReceivedError(view, request, error);
                if (request != null && request.isForMainFrame()) {
                    String fallbackUrl = getString(R.string.fallback_server_url);
                    SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
                    boolean customized = sp.getBoolean(KEY_USER_CUSTOMIZED, false);
                    if (!customized && !triedFallbackOnColdLoad && !currentBaseUrl.equals(fallbackUrl)) {
                        triedFallbackOnColdLoad = true;
                        currentBaseUrl = fallbackUrl;
                        view.loadUrl(currentBaseUrl + "/console");
                        return;
                    }
                    String desc = "网络连接不可用";
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && error != null && error.getDescription() != null) {
                        desc = error.getDescription().toString();
                    }
                    offlineDetailText.setText("当前地址：" + currentBaseUrl + "\n原因：" + desc);
                    offlineOverlay.setVisibility(View.VISIBLE);
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                if (progressBar == null) return;
                if (newProgress >= 100) {
                    progressBar.setVisibility(View.GONE);
                } else {
                    if (progressBar.getVisibility() != View.VISIBLE) {
                        progressBar.setVisibility(View.VISIBLE);
                    }
                    progressBar.setProgress(newProgress);
                }
            }

            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> filePathCallback, FileChooserParams fileChooserParams) {
                if (fileUploadCallback != null) {
                    fileUploadCallback.onReceiveValue(null);
                }
                fileUploadCallback = filePathCallback;

                boolean captureRequested = fileChooserParams != null && fileChooserParams.isCaptureEnabled();
                if (captureRequested) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                            && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAMERA_CAPTURE_PERM);
                        return true;
                    }
                    return launchNativeCameraCapture();
                }

                Intent contentIntent = new Intent(Intent.ACTION_GET_CONTENT);
                contentIntent.addCategory(Intent.CATEGORY_OPENABLE);
                contentIntent.setType("image/*");
                if (fileChooserParams != null && fileChooserParams.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE) {
                    contentIntent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                }
                Intent chooser = Intent.createChooser(contentIntent, "选择图片附加到记忆或随记");
                try {
                    startActivityForResult(chooser, REQ_FILE_CHOOSER);
                    return true;
                } catch (Throwable e) {
                    fileUploadCallback = null;
                    return false;
                }
            }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (request == null) return;
                        String[] resources = request.getResources();
                        List<String> osPermsNeeded = new ArrayList<String>();
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && resources != null) {
                            for (int i = 0; i < resources.length; i++) {
                                String r = resources[i];
                                if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) {
                                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                                        osPermsNeeded.add(Manifest.permission.RECORD_AUDIO);
                                    }
                                } else if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) {
                                    if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                                        osPermsNeeded.add(Manifest.permission.CAMERA);
                                    }
                                }
                            }
                        }
                        if (osPermsNeeded.isEmpty() || Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
                            request.grant(resources);
                        } else {
                            pendingWebPermissionRequest = request;
                            requestPermissions(osPermsNeeded.toArray(new String[0]), REQ_WEB_PERMISSIONS);
                        }
                    }
                });
            }
        });
    }

    private void injectNativeEnhancementBridge() {
        if (webView == null) return;
        String js = "(function(){"
                + "if(window.__ttAndroidBridgeInstalled)return;"
                + "window.__ttAndroidBridgeInstalled=true;"
                + "function syncBar(){"
                + "  try{"
                + "    var bg=getComputedStyle(document.body).backgroundColor||'';"
                + "    if(window.TraceTodoNative&&window.TraceTodoNative.onThemeBgChanged){"
                + "      window.TraceTodoNative.onThemeBgChanged(bg);"
                + "    }"
                + "  }catch(e){}"
                + "}"
                + "function syncAuthForWidget(){"
                + "  try{"
                + "    var tok=localStorage.getItem('tm_auth_token')||'';"
                + "    if(window.TraceTodoNative&&window.TraceTodoNative.onAuthTokenSynced){"
                + "      window.TraceTodoNative.onAuthTokenSynced(tok);"
                + "    }"
                + "  }catch(e){}"
                + "}"
                + "syncBar();"
                + "syncAuthForWidget();"
                + "setTimeout(syncBar,180);"
                + "setTimeout(syncAuthForWidget,900);"
                + "setInterval(syncAuthForWidget,5000);"
                + "new MutationObserver(function(){setTimeout(syncBar,40);})"
                + ".observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});"
                + "var fo=document.getElementById('focusOverlay');"
                + "if(fo){"
                + "  new MutationObserver(function(){"
                + "    if(window.TraceTodoNative&&window.TraceTodoNative.setKeepScreenOn){"
                + "      window.TraceTodoNative.setKeepScreenOn(Boolean(fo.open));"
                + "    }"
                + "  }).observe(fo,{attributes:true,attributeFilter:['open']});"
                + "}"
                + "document.addEventListener('click',function(ev){"
                + "  var btn=ev.target&&ev.target.closest?ev.target.closest('button,.scene-card-item,.theme-card-item'):null;"
                + "  if(btn&&window.TraceTodoNative&&window.TraceTodoNative.lightTap){"
                + "    window.TraceTodoNative.lightTap();"
                + "  }"
                + "  var a=ev.target&&ev.target.closest?ev.target.closest('a[download]'):null;"
                + "  if(a&&a.href&&a.href.indexOf('blob:')===0&&window.TraceTodoNative&&window.TraceTodoNative.shareExportedText){"
                + "    ev.preventDefault();"
                + "    var fname=a.getAttribute('download')||'tracetodo-export.md';"
                + "    fetch(a.href).then(function(r){return r.text();}).then(function(txt){"
                + "      window.TraceTodoNative.shareExportedText(fname,txt);"
                + "    }).catch(function(){});"
                + "  }"
                + "},true);"
                + "})();";
        webView.evaluateJavascript(js, null);
    }

    private void dispatchPendingIntentActions() {
        if (webView == null) return;
        if (pendingSharedText != null && !pendingSharedText.isEmpty()) {
            final String textToInject = pendingSharedText;
            pendingSharedText = null;
            String quoted = JSONObject.quote(textToInject);
            final String js = "(function(){"
                    + "if(typeof switchTab==='function')switchTab('tasks');"
                    + "var inp=document.getElementById('quickInput')||document.querySelector('input.main-input');"
                    + "if(inp){inp.value=" + quoted + ";inp.dispatchEvent(new Event('input',{bubbles:true}));inp.focus();}"
                    + "if(typeof showToast==='function')showToast('✨ 已带入分享文本，点击「AI 录入」即可归档');"
                    + "})();";
            webView.postDelayed(new Runnable() {
                @Override
                public void run() {
                    if (webView != null) webView.evaluateJavascript(js, null);
                }
            }, 280);
        }
        if (pendingDeepAction != null && !pendingDeepAction.isEmpty()) {
            final String act = pendingDeepAction;
            pendingDeepAction = null;
            final String js;
            if ("focus".equals(act)) {
                js = "if(typeof switchTab==='function')switchTab('focus');";
            } else if ("calendar".equals(act) || "countdown".equals(act)) {
                js = "if(typeof switchTab==='function')switchTab('calendar');";
            } else if ("notes".equals(act) || "memo".equals(act) || "journal".equals(act)) {
                js = "if(typeof switchTab==='function')switchTab('notes');";
            } else if ("login".equals(act) || "auth".equals(act) || "account".equals(act)) {
                js = "if(typeof openAuthModal==='function')openAuthModal();";
            } else if ("ask".equals(act)) {
                js = "if(typeof toggleAskFloatPanel==='function')toggleAskFloatPanel();else if(typeof switchTab==='function')switchTab('ask');";
            } else {
                js = "if(typeof switchTab==='function')switchTab('tasks');var q=document.getElementById('quickInput')||document.querySelector('input.main-input');if(q)q.focus();";
            }
            webView.postDelayed(new Runnable() {
                @Override
                public void run() {
                    if (webView != null) webView.evaluateJavascript(js, null);
                }
            }, 260);
        }
    }

    public final class NativeBridge {
        @JavascriptInterface
        public void onThemeBgChanged(final String cssColor) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    Integer parsed = parseCssColor(cssColor);
                    if (parsed != null) {
                        applySystemBarColorsInt(parsed.intValue());
                        if (rootLayout != null) rootLayout.setBackgroundColor(parsed.intValue());
                        if (webView != null) webView.setBackgroundColor(parsed.intValue());
                        getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                                .edit()
                                .putInt(KEY_LAST_THEME_BG, parsed.intValue())
                                .apply();
                    }
                }
            });
        }

        @JavascriptInterface
        public void setKeepScreenOn(final boolean keepOn) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    try {
                        Window w = getWindow();
                        if (w == null) return;
                        if (keepOn) {
                            w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                        } else {
                            w.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                        }
                    } catch (Throwable ignored) {
                    }
                }
            });
        }

        @JavascriptInterface
        public void lightTap() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    try {
                        if (webView != null) {
                            webView.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK);
                        }
                    } catch (Throwable ignored) {
                    }
                }
            });
        }

        @JavascriptInterface
        public void shareExportedText(final String filename, final String content) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    if (content == null || content.isEmpty()) return;
                    Intent send = new Intent(Intent.ACTION_SEND);
                    send.setType("text/plain");
                    send.putExtra(Intent.EXTRA_SUBJECT, filename != null ? filename : "TraceTodo-Export.md");
                    send.putExtra(Intent.EXTRA_TEXT, content);
                    try {
                        startActivity(Intent.createChooser(send, "导出 / 分享知识库 Markdown"));
                    } catch (Throwable ignored) {
                    }
                }
            });
        }

        @JavascriptInterface
        public void onAuthTokenSynced(final String token) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    String clean = token == null ? "" : token.trim();
                    SharedPreferences sp = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
                    String prev = sp.getString(KEY_AUTH_TOKEN, "");
                    if (!clean.equals(prev)) {
                        sp.edit().putString(KEY_AUTH_TOKEN, clean).apply();
                        TodoWidgetProvider.requestImmediateRefresh(MainActivity.this);
                        MemoWidgetProvider.requestImmediateRefresh(MainActivity.this);
                        CalendarWidgetProvider.requestImmediateRefresh(MainActivity.this);
                    }
                }
            });
        }

        @JavascriptInterface
        public void openServerSwitcher() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    showServerSwitchDialog();
                }
            });
        }
    }

    private Integer parseCssColor(String raw) {
        if (raw == null) return null;
        String s = raw.trim();
        try {
            if (s.startsWith("#")) {
                return Integer.valueOf(Color.parseColor(s));
            }
            if (s.startsWith("rgb")) {
                int l = s.indexOf('(');
                int r = s.indexOf(')');
                if (l > 0 && r > l) {
                    String[] parts = s.substring(l + 1, r).split(",");
                    if (parts.length >= 3) {
                        int red = Integer.parseInt(parts[0].trim());
                        int green = Integer.parseInt(parts[1].trim());
                        int blue = Integer.parseInt(parts[2].trim());
                        return Integer.valueOf(Color.rgb(red, green, blue));
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    @SuppressWarnings("deprecation")
    private void applySystemBarColorsInt(int color) {
        try {
            Window window = getWindow();
            if (window == null) return;
            window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
            window.clearFlags(WindowManager.LayoutParams.FLAG_TRANSLUCENT_STATUS);
            window.setStatusBarColor(color);
            window.setNavigationBarColor(color);

            double lum = (0.299 * Color.red(color) + 0.587 * Color.green(color) + 0.114 * Color.blue(color)) / 255.0;
            boolean isLightBg = lum > 0.55;

            View decor = window.getDecorView();
            if (decor != null) {
                int flags = decor.getSystemUiVisibility();
                if (isLightBg) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                        flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                    }
                } else {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                        flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                    }
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                        flags &= ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                    }
                }
                decor.setSystemUiVisibility(flags);
            }
        } catch (Throwable ignored) {
        }
    }

    private void showServerSwitchDialog() {
        final String prodHttps = getString(R.string.default_server_url);
        final String prodIp = getString(R.string.fallback_server_url);
        final String testIp = getString(R.string.test_server_url);

        LinearLayout container = new LinearLayout(this);
        container.setOrientation(LinearLayout.VERTICAL);
        container.setPadding(dp(20), dp(12), dp(20), dp(4));

        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(currentBaseUrl);
        input.setSelection(currentBaseUrl.length());
        container.addView(input);

        String[] presets = new String[]{
                "☁️ 线上正式域名 (" + prodHttps + ")",
                "🌐 线上正式直连 (" + prodIp + ")",
                "🧪 云端测试环境 (" + testIp + ")"
        };

        new AlertDialog.Builder(this)
                .setTitle("切换 有迹 服务端环境")
                .setItems(presets, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        if (which == 0) applyNewServerUrl(prodHttps, false);
                        else if (which == 1) applyNewServerUrl(prodIp, true);
                        else if (which == 2) applyNewServerUrl(testIp, true);
                    }
                })
                .setView(container)
                .setPositiveButton("保存输入的地址", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        String entered = input.getText() != null ? input.getText().toString() : "";
                        if (!entered.trim().isEmpty()) {
                            applyNewServerUrl(entered, true);
                        }
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void applyNewServerUrl(String rawUrl, boolean userCustomized) {
        String cleaned = cleanServerUrl(rawUrl);
        currentBaseUrl = cleaned;
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                .edit()
                .putString(KEY_SERVER_URL, cleaned)
                .putBoolean(KEY_USER_CUSTOMIZED, userCustomized)
                .apply();
        if (offlineOverlay != null) {
            offlineOverlay.setVisibility(View.GONE);
        }
        loadServerHome(true);
    }

    private String cleanServerUrl(String raw) {
        String s = raw == null ? "" : raw.trim();
        while (s.endsWith("/")) {
            s = s.substring(0, s.length() - 1);
        }
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            s = "https://" + s;
        }
        return s;
    }

    private void loadServerHome(boolean isRetry) {
        if (!isRetry) {
            triedFallbackOnColdLoad = false;
        }
        if (webView != null) {
            webView.loadUrl(currentBaseUrl + "/console");
        }
    }

    private void requestNotificationPermissionIfNeeded() {
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_POST_NOTIF);
                }
            }
        } catch (Throwable ignored) {
        }
    }

    private boolean launchNativeCameraCapture() {
        try {
            pendingCameraFile = CameraFileProvider.prepareCaptureFile(this);
            pendingCameraUri = CameraFileProvider.getUriForFile(this, pendingCameraFile);
            Intent captureIntent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
            captureIntent.putExtra(MediaStore.EXTRA_OUTPUT, pendingCameraUri);
            captureIntent.setClipData(ClipData.newRawUri("", pendingCameraUri));
            captureIntent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivityForResult(captureIntent, REQ_CAMERA_CAPTURE);
            return true;
        } catch (Throwable e) {
            Toast.makeText(this, "无法打开系统相机，请从相册选择图片", Toast.LENGTH_SHORT).show();
            if (fileUploadCallback != null) {
                fileUploadCallback.onReceiveValue(null);
                fileUploadCallback = null;
            }
            pendingCameraFile = null;
            pendingCameraUri = null;
            return false;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_CAMERA_CAPTURE) {
            if (fileUploadCallback != null) {
                Uri[] results = null;
                if (resultCode == Activity.RESULT_OK) {
                    if (pendingCameraFile != null && pendingCameraFile.exists() && pendingCameraFile.length() > 0 && pendingCameraUri != null) {
                        results = new Uri[]{pendingCameraUri};
                    } else if (data != null && data.getData() != null) {
                        results = new Uri[]{data.getData()};
                    }
                }
                fileUploadCallback.onReceiveValue(results);
                fileUploadCallback = null;
            }
            pendingCameraFile = null;
            pendingCameraUri = null;
            return;
        }
        if (requestCode == REQ_FILE_CHOOSER) {
            if (fileUploadCallback == null) return;
            Uri[] results = null;
            if (resultCode == Activity.RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    ClipData clip = data.getClipData();
                    results = new Uri[clip.getItemCount()];
                    for (int i = 0; i < clip.getItemCount(); i++) {
                        results[i] = clip.getItemAt(i).getUri();
                    }
                } else if (data.getData() != null) {
                    results = new Uri[]{data.getData()};
                }
            }
            fileUploadCallback.onReceiveValue(results);
            fileUploadCallback = null;
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_CAMERA_CAPTURE_PERM) {
            boolean granted = grantResults != null
                    && grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            if (granted && fileUploadCallback != null) {
                launchNativeCameraCapture();
            } else {
                Toast.makeText(this, "未授予相机权限，无法直接拍摄照片", Toast.LENGTH_SHORT).show();
                if (fileUploadCallback != null) {
                    fileUploadCallback.onReceiveValue(null);
                    fileUploadCallback = null;
                }
            }
            return;
        }
        if (requestCode == REQ_WEB_PERMISSIONS && pendingWebPermissionRequest != null) {
            boolean allGranted = grantResults != null && grantResults.length > 0;
            if (grantResults != null) {
                for (int i = 0; i < grantResults.length; i++) {
                    if (grantResults[i] != PackageManager.PERMISSION_GRANTED) {
                        allGranted = false;
                        break;
                    }
                }
            }
            if (allGranted) {
                pendingWebPermissionRequest.grant(pendingWebPermissionRequest.getResources());
            } else {
                pendingWebPermissionRequest.deny();
                Toast.makeText(this, "未授予麦克风/相机权限，语音或拍照功能暂不可用", Toast.LENGTH_SHORT).show();
            }
            pendingWebPermissionRequest = null;
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && event.getAction() == KeyEvent.ACTION_DOWN) {
            handleSmartBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private void handleSmartBack() {
        if (webView == null || (offlineOverlay != null && offlineOverlay.getVisibility() == View.VISIBLE)) {
            finish();
            return;
        }
        String closeModalJs = "(function(){"
                + "var fo=document.getElementById('focusOverlay');"
                + "if(fo&&fo.open){if(typeof exitImmersiveLock==='function')exitImmersiveLock();else fo.close();return 'closed';}"
                + "var ids=['mobileImageSourceModal','confirmDialogModal','mediaLightboxModal','noteDetailModal','detailModal','prefsModal','calSlotModal','countdownEditorModal','askFloatPanel'];"
                + "for(var i=0;i<ids.length;i++){"
                + "  var el=document.getElementById(ids[i]);"
                + "  if(el&&el.classList.contains('open')){el.classList.remove('open');return 'closed';}"
                + "}"
                + "return 'none';"
                + "})();";
        webView.evaluateJavascript(closeModalJs, new ValueCallback<String>() {
            @Override
            public void onReceiveValue(String value) {
                if (value != null && value.contains("closed")) {
                    return;
                }
                if (webView != null && webView.canGoBack()) {
                    webView.goBack();
                    return;
                }
                long now = System.currentTimeMillis();
                if (now - lastBackPressMs < 2000L) {
                    finish();
                } else {
                    lastBackPressMs = now;
                    Toast.makeText(MainActivity.this, "再按一次返回退出 有迹", Toast.LENGTH_SHORT).show();
                }
            }
        });
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onPause() {
        if (webView != null) webView.onPause();
        TodoWidgetProvider.requestImmediateRefresh(this);
        MemoWidgetProvider.requestImmediateRefresh(this);
        CalendarWidgetProvider.requestImmediateRefresh(this);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            try {
                webView.destroy();
            } catch (Throwable ignored) {
            }
        }
        super.onDestroy();
    }
}
