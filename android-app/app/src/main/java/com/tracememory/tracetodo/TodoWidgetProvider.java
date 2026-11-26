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

public class TodoWidgetProvider extends AppWidgetProvider {

    public static final String ACTION_WIDGET_REFRESH = "com.tracememory.tracetodo.ACTION_WIDGET_REFRESH";
    private static final String PREFS_NAME = "tracetodo_native_prefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_AUTH_TOKEN = "auth_token";
    private static final String KEY_WIDGET_CACHE = "widget_matrix_json";

    public static void requestImmediateRefresh(Context context) {
        if (context == null) return;
        try {
            AppWidgetManager mgr = AppWidgetManager.getInstance(context);
            int[] ids = mgr.getAppWidgetIds(new ComponentName(context, TodoWidgetProvider.class));
            if (ids != null && ids.length > 0) {
                refreshAllAsync(context.getApplicationContext());
            }
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (intent != null && ACTION_WIDGET_REFRESH.equals(intent.getAction())) {
            refreshAllAsync(context.getApplicationContext());
        }
    }

    @Override
    public void onUpdate(Context context, AppWidgetManager appWidgetManager, int[] appWidgetIds) {
        SharedPreferences sp = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String cached = sp.getString(KEY_WIDGET_CACHE, "");
        String tok = sp.getString(KEY_AUTH_TOKEN, "");
        renderAllWidgets(context, appWidgetManager, appWidgetIds, cached, tok != null && !tok.isEmpty(), false);
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

                String apiUrl = baseUrl + "/api/tasks/matrix?sort_by=priority&include_done=false";
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
                        sp.edit().putString(KEY_WIDGET_CACHE, freshJson).apply();
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

                String jsonToRender = freshJson != null ? freshJson : sp.getString(KEY_WIDGET_CACHE, "");
                try {
                    AppWidgetManager mgr = AppWidgetManager.getInstance(appCtx);
                    int[] ids = mgr.getAppWidgetIds(new ComponentName(appCtx, TodoWidgetProvider.class));
                    if (ids != null && ids.length > 0) {
                        renderAllWidgets(appCtx, mgr, ids, jsonToRender, !token.isEmpty(), needsLogin);
                    }
                } catch (Throwable ignored) {
                }
            }
        }).start();
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

        Intent openMain = new Intent(Intent.ACTION_VIEW, Uri.parse(needsLogin ? "tracetodo://login" : "tracetodo://tasks"));
        openMain.setClass(context, MainActivity.class);
        PendingIntent openMainPi = PendingIntent.getActivity(context, 10, openMain, piFlags);

        Intent openCapture = new Intent(Intent.ACTION_VIEW, Uri.parse("tracetodo://capture"));
        openCapture.setClass(context, MainActivity.class);
        PendingIntent capturePi = PendingIntent.getActivity(context, 11, openCapture, piFlags);

        Intent openFocus = new Intent(Intent.ACTION_VIEW, Uri.parse("tracetodo://focus"));
        openFocus.setClass(context, MainActivity.class);
        PendingIntent focusPi = PendingIntent.getActivity(context, 13, openFocus, piFlags);

        Intent refreshIntent = new Intent(context, TodoWidgetProvider.class);
        refreshIntent.setAction(ACTION_WIDGET_REFRESH);
        PendingIntent refreshPi = PendingIntent.getBroadcast(context, 12, refreshIntent, piFlags);

        int totalTodo = 0;
        int q1 = 0;
        int q2 = 0;
        String[] topTitles = new String[3];
        int topCount = 0;
        boolean parsedOk = false;

        if (jsonStr != null && !jsonStr.trim().isEmpty()) {
            try {
                JSONObject root = new JSONObject(jsonStr);
                JSONObject summary = root.optJSONObject("summary_counts");
                if (summary != null) {
                    totalTodo = summary.optInt("total_todo", 0);
                    q1 = summary.optInt("Q1_do_first", 0);
                    q2 = summary.optInt("Q2_schedule", 0);
                }
                JSONArray arr = root.optJSONArray("sorted_tasks");
                if (arr != null) {
                    for (int i = 0; i < arr.length() && topCount < 3; i++) {
                        JSONObject item = arr.optJSONObject(i);
                        if (item == null) continue;
                        JSONObject payload = item.optJSONObject("payload");
                        String st = payload != null ? payload.optString("task_status", "todo") : "todo";
                        if ("done".equals(st) || "canceled".equals(st)) continue;
                        String q = payload != null ? payload.optString("quadrant", "Q4") : "Q4";
                        String badge = q.length() >= 2 ? q.substring(0, 2) : "Q4";
                        String title = item.optString("title", "").trim();
                        if (!title.isEmpty()) {
                            topTitles[topCount++] = "[" + badge + "] " + title;
                        }
                    }
                }
                parsedOk = true;
            } catch (Throwable ignored) {
            }
        }

        for (int i = 0; i < ids.length; i++) {
            RemoteViews rv = new RemoteViews(context.getPackageName(), R.layout.widget_todo);
            rv.setOnClickPendingIntent(R.id.widget_root, openMainPi);
            rv.setOnClickPendingIntent(R.id.widget_add_btn, capturePi);
            rv.setOnClickPendingIntent(R.id.widget_focus_btn, focusPi);
            rv.setOnClickPendingIntent(R.id.widget_refresh_btn, refreshPi);

            rv.setTextViewText(R.id.widget_total_count, String.valueOf(totalTodo));
            rv.setTextViewText(R.id.widget_q_summary, "Q1: " + q1 + " · Q2: " + q2);

            if (!parsedOk && (needsLogin || !hasToken)) {
                rv.setTextViewText(R.id.widget_task_1, "🔐 轻点打开 有迹 登录同步待办");
                rv.setViewVisibility(R.id.widget_task_2, View.GONE);
                rv.setViewVisibility(R.id.widget_task_3, View.GONE);
            } else if (topCount == 0) {
                rv.setTextViewText(R.id.widget_task_1, parsedOk ? "🎉 今日待办已全部清空" : "轻点打开应用同步最新待办");
                rv.setViewVisibility(R.id.widget_task_2, View.GONE);
                rv.setViewVisibility(R.id.widget_task_3, View.GONE);
            } else {
                rv.setTextViewText(R.id.widget_task_1, topTitles[0]);
                if (topCount >= 2) {
                    rv.setTextViewText(R.id.widget_task_2, topTitles[1]);
                    rv.setViewVisibility(R.id.widget_task_2, View.VISIBLE);
                } else {
                    rv.setViewVisibility(R.id.widget_task_2, View.GONE);
                }
                if (topCount >= 3) {
                    rv.setTextViewText(R.id.widget_task_3, topTitles[2]);
                    rv.setViewVisibility(R.id.widget_task_3, View.VISIBLE);
                } else {
                    rv.setViewVisibility(R.id.widget_task_3, View.GONE);
                }
            }

            mgr.updateAppWidget(ids[i], rv);
        }
    }
}
