package com.tracememory.tracetodo;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

public class MemoWidgetProvider extends AppWidgetProvider {

    public static final String ACTION_MEMO_WIDGET_REFRESH = "com.tracememory.tracetodo.ACTION_MEMO_WIDGET_REFRESH";
    private static final String PREFS_NAME = "tracetodo_native_prefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_AUTH_TOKEN = "auth_token";
    private static final String KEY_MEMO_CACHE = "widget_memos_json";

    public static void requestImmediateRefresh(Context context) {
        if (context == null) return;
        try {
            AppWidgetManager mgr = AppWidgetManager.getInstance(context);
            int[] ids = mgr.getAppWidgetIds(new ComponentName(context, MemoWidgetProvider.class));
            if (ids != null && ids.length > 0) {
                refreshAllAsync(context.getApplicationContext());
            }
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (intent != null && ACTION_MEMO_WIDGET_REFRESH.equals(intent.getAction())) {
            refreshAllAsync(context.getApplicationContext());
        }
    }

    @Override
    public void onUpdate(Context context, AppWidgetManager appWidgetManager, int[] appWidgetIds) {
        SharedPreferences sp = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String cached = sp.getString(KEY_MEMO_CACHE, "");
        String tok = sp.getString(KEY_AUTH_TOKEN, "");
        renderAllWidgets(context, appWidgetManager, appWidgetIds, cached, tok != null && !tok.trim().isEmpty(), false);
        refreshAllAsync(context.getApplicationContext());
    }

    private static void refreshAllAsync(final Context appCtx) {
        new Thread(new Runnable() {
            @Override
            public void run() {
                SharedPreferences sp = appCtx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
                String baseUrl = sp.getString(KEY_SERVER_URL, "");
                if (baseUrl == null || baseUrl.trim().isEmpty()) {
                    baseUrl = appCtx.getString(R.string.default_server_url);
                }
                baseUrl = baseUrl.trim();
                while (baseUrl.endsWith("/")) {
                    baseUrl = baseUrl.substring(0, baseUrl.length() - 1);
                }
                String token = sp.getString(KEY_AUTH_TOKEN, "");
                if (token == null) token = "";
                token = token.trim();

                String apiUrl = baseUrl + "/api/memos/list?limit=5";
                if (token.isEmpty()) {
                    apiUrl += "&space_id=default";
                }

                boolean needsLogin = false;
                String freshJson = null;
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(apiUrl).openConnection();
                    conn.setRequestMethod("GET");
                    conn.setConnectTimeout(7000);
                    conn.setReadTimeout(7000);
                    if (!token.isEmpty()) {
                        conn.setRequestProperty("Authorization", "Bearer " + token);
                    }
                    int code = conn.getResponseCode();
                    if (code >= 200 && code < 300) {
                        InputStream is = conn.getInputStream();
                        BufferedReader reader = new BufferedReader(new InputStreamReader(is, "UTF-8"));
                        StringBuilder sb = new StringBuilder();
                        String line;
                        while ((line = reader.readLine()) != null) {
                            sb.append(line);
                        }
                        reader.close();
                        freshJson = sb.toString();
                        sp.edit().putString(KEY_MEMO_CACHE, freshJson).apply();
                    } else if (code == 401 || code == 403) {
                        needsLogin = true;
                    }
                } catch (Throwable ignored) {
                } finally {
                    if (conn != null) {
                        try {
                            conn.disconnect();
                        } catch (Throwable ignored) {
                        }
                    }
                }

                String jsonToRender = freshJson != null ? freshJson : sp.getString(KEY_MEMO_CACHE, "");
                try {
                    AppWidgetManager mgr = AppWidgetManager.getInstance(appCtx);
                    int[] ids = mgr.getAppWidgetIds(new ComponentName(appCtx, MemoWidgetProvider.class));
                    if (ids != null && ids.length > 0) {
                        renderAllWidgets(appCtx, mgr, ids, jsonToRender, !token.isEmpty(), needsLogin);
                    }
                } catch (Throwable ignored) {
                }
            }
        }).start();
    }

    private static String stripMarkdown(String raw) {
        if (raw == null) return "";
        return raw.replaceAll("(?m)^#{1,6}\\s*", "")
                .replaceAll("(?m)^[-*•]\\s*(\\[[ xX]\\]\\s*)?", "")
                .replaceAll("(?m)^>\\s*", "")
                .replace("**", "")
                .replace("`", "")
                .replace('\n', ' ')
                .replaceAll("\\s+", " ")
                .trim();
    }

    private static void renderAllWidgets(
            Context context,
            AppWidgetManager mgr,
            int[] ids,
            String jsonStr,
            boolean hasToken,
            boolean needsLogin
    ) {
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            piFlags |= PendingIntent.FLAG_IMMUTABLE;
        }

        Intent openNotes = new Intent(Intent.ACTION_VIEW, Uri.parse(needsLogin ? "tracetodo://login" : "tracetodo://notes"));
        openNotes.setClass(context, MainActivity.class);
        PendingIntent openNotesPi = PendingIntent.getActivity(context, 20, openNotes, piFlags);

        Intent addNote = new Intent(Intent.ACTION_VIEW, Uri.parse("tracetodo://notes"));
        addNote.setClass(context, MainActivity.class);
        PendingIntent addNotePi = PendingIntent.getActivity(context, 21, addNote, piFlags);

        Intent refreshIntent = new Intent(context, MemoWidgetProvider.class);
        refreshIntent.setAction(ACTION_MEMO_WIDGET_REFRESH);
        PendingIntent refreshPi = PendingIntent.getBroadcast(context, 22, refreshIntent, piFlags);

        int totalMemos = 0;
        int pinnedCount = 0;
        String[] rows = new String[3];
        int rowCount = 0;
        boolean parsedOk = false;

        if (jsonStr != null && !jsonStr.trim().isEmpty()) {
            try {
                JSONObject root = new JSONObject(jsonStr);
                totalMemos = root.optInt("total", 0);
                pinnedCount = root.optInt("pinned_count", 0);
                JSONArray arr = root.optJSONArray("memos");
                if (arr != null) {
                    if (totalMemos == 0) totalMemos = arr.length();
                    for (int i = 0; i < arr.length() && rowCount < 3; i++) {
                        JSONObject item = arr.optJSONObject(i);
                        if (item == null) continue;
                        boolean pinned = item.optBoolean("pinned", false);
                        String title = item.optString("title", "").trim();
                        String body = stripMarkdown(item.optString("content", ""));
                        String line;
                        if (!title.isEmpty() && !body.isEmpty()) {
                            line = (pinned ? "📌 " : "• ") + title + " — " + body;
                        } else if (!title.isEmpty()) {
                            line = (pinned ? "📌 " : "• ") + title;
                        } else {
                            line = (pinned ? "📌 " : "• ") + (body.isEmpty() ? "随记便签" : body);
                        }
                        rows[rowCount++] = line;
                    }
                }
                parsedOk = true;
            } catch (Throwable ignored) {
            }
        }

        for (int i = 0; i < ids.length; i++) {
            RemoteViews rv = new RemoteViews(context.getPackageName(), R.layout.widget_memo);
            rv.setOnClickPendingIntent(R.id.memo_widget_root, openNotesPi);
            rv.setOnClickPendingIntent(R.id.memo_widget_add_btn, addNotePi);
            rv.setOnClickPendingIntent(R.id.memo_widget_refresh_btn, refreshPi);

            rv.setTextViewText(R.id.memo_widget_total, String.valueOf(totalMemos));
            rv.setTextViewText(R.id.memo_widget_pinned, "置顶: " + pinnedCount + " 条");

            if (!parsedOk && (needsLogin || !hasToken)) {
                rv.setTextViewText(R.id.memo_widget_row_1, "🔐 轻点打开 有迹 登录同步随记");
                rv.setViewVisibility(R.id.memo_widget_row_2, View.GONE);
                rv.setViewVisibility(R.id.memo_widget_row_3, View.GONE);
            } else if (rowCount == 0) {
                rv.setTextViewText(R.id.memo_widget_row_1, parsedOk ? "✨ 暂无备忘录，点击左侧「记灵感」写下第一篇" : "轻点打开应用同步随记备忘录");
                rv.setViewVisibility(R.id.memo_widget_row_2, View.GONE);
                rv.setViewVisibility(R.id.memo_widget_row_3, View.GONE);
            } else {
                rv.setTextViewText(R.id.memo_widget_row_1, rows[0]);
                if (rowCount >= 2) {
                    rv.setTextViewText(R.id.memo_widget_row_2, rows[1]);
                    rv.setViewVisibility(R.id.memo_widget_row_2, View.VISIBLE);
                } else {
                    rv.setViewVisibility(R.id.memo_widget_row_2, View.GONE);
                }
                if (rowCount >= 3) {
                    rv.setTextViewText(R.id.memo_widget_row_3, rows[2]);
                    rv.setViewVisibility(R.id.memo_widget_row_3, View.VISIBLE);
                } else {
                    rv.setViewVisibility(R.id.memo_widget_row_3, View.GONE);
                }
            }

            mgr.updateAppWidget(ids[i], rv);
        }
    }
}
