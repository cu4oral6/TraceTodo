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
import java.util.Calendar;

public class CalendarWidgetProvider extends AppWidgetProvider {

    public static final String ACTION_CAL_WIDGET_REFRESH = "com.tracememory.tracetodo.ACTION_CAL_WIDGET_REFRESH";
    private static final String PREFS_NAME = "tracetodo_native_prefs";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_AUTH_TOKEN = "auth_token";
    private static final String KEY_CAL_CACHE = "widget_calendar_json";

    public static void requestImmediateRefresh(Context context) {
        if (context == null) return;
        try {
            AppWidgetManager mgr = AppWidgetManager.getInstance(context);
            int[] ids = mgr.getAppWidgetIds(new ComponentName(context, CalendarWidgetProvider.class));
            if (ids != null && ids.length > 0) {
                refreshAllAsync(context.getApplicationContext());
            }
        } catch (Throwable ignored) {
        }
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (intent != null && ACTION_CAL_WIDGET_REFRESH.equals(intent.getAction())) {
            refreshAllAsync(context.getApplicationContext());
        }
    }

    @Override
    public void onUpdate(Context context, AppWidgetManager appWidgetManager, int[] appWidgetIds) {
        SharedPreferences sp = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String cached = sp.getString(KEY_CAL_CACHE, "");
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

                String apiUrl = baseUrl + "/api/calendar/overview?view=week&include_done=true";
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
                        sp.edit().putString(KEY_CAL_CACHE, freshJson).apply();
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

                String jsonToRender = freshJson != null ? freshJson : sp.getString(KEY_CAL_CACHE, "");
                try {
                    AppWidgetManager mgr = AppWidgetManager.getInstance(appCtx);
                    int[] ids = mgr.getAppWidgetIds(new ComponentName(appCtx, CalendarWidgetProvider.class));
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

        Intent openCal = new Intent(Intent.ACTION_VIEW, Uri.parse(needsLogin ? "tracetodo://login" : "tracetodo://calendar"));
        openCal.setClass(context, MainActivity.class);
        PendingIntent openCalPi = PendingIntent.getActivity(context, 30, openCal, piFlags);

        Intent refreshIntent = new Intent(context, CalendarWidgetProvider.class);
        refreshIntent.setAction(ACTION_CAL_WIDGET_REFRESH);
        PendingIntent refreshPi = PendingIntent.getBroadcast(context, 31, refreshIntent, piFlags);

        Calendar cal = Calendar.getInstance();
        int monthNum = cal.get(Calendar.MONTH) + 1;
        int dayNum = cal.get(Calendar.DAY_OF_MONTH);
        int dow = cal.get(Calendar.DAY_OF_WEEK);
        String[] wds = new String[]{"周日", "周一", "周二", "周三", "周四", "周五", "周六"};
        String wdLabel = wds[Math.max(0, Math.min(6, dow - 1))];
        boolean isWeekend = (dow == Calendar.SUNDAY || dow == Calendar.SATURDAY);
        String lunarShort = "";
        String subLabel = "";
        String solarTerm = "";
        String festLabel = "";
        String holidayStatus = "";
        boolean isOffDay = isWeekend;
        int totalToday = 0;
        int focusMins = 0;
        String[] rows = new String[3];
        int rowCount = 0;
        boolean parsedOk = false;

        if (jsonStr != null && !jsonStr.trim().isEmpty()) {
            try {
                JSONObject root = new JSONObject(jsonStr);
                JSONArray days = root.optJSONArray("days");
                if (days != null && days.length() > 0) {
                    JSONObject todayObj = null;
                    for (int i = 0; i < days.length(); i++) {
                        JSONObject d = days.optJSONObject(i);
                        if (d != null && d.optBoolean("is_today", false)) {
                            todayObj = d;
                            break;
                        }
                    }
                    if (todayObj == null) todayObj = days.optJSONObject(0);
                    if (todayObj != null) {
                        monthNum = todayObj.optInt("month", monthNum);
                        dayNum = todayObj.optInt("day", dayNum);
                        String w = todayObj.optString("weekday_label", "");
                        if (!w.trim().isEmpty()) wdLabel = w.trim();
                        focusMins = todayObj.optInt("focus_minutes", 0);

                        isWeekend = todayObj.optBoolean("is_weekend", isWeekend);
                        lunarShort = todayObj.optString("lunar_short", "").trim();
                        subLabel = todayObj.optString("calendar_sub_label", "").trim();
                        solarTerm = todayObj.optString("solar_term", "").trim();
                        holidayStatus = todayObj.optString("holiday_status", "").trim();
                        JSONArray fests = todayObj.optJSONArray("festivals");
                        if (fests != null && fests.length() > 0) {
                            festLabel = fests.optString(0, "").trim();
                        }
                        if (todayObj.has("is_off_day") && !todayObj.isNull("is_off_day")) {
                            isOffDay = todayObj.optBoolean("is_off_day", isOffDay);
                        } else {
                            isOffDay = "rest".equals(holidayStatus) || (isWeekend && !"work".equals(holidayStatus));
                        }

                        JSONArray timed = todayObj.optJSONArray("timed_blocks");
                        if (timed != null) {
                            totalToday += timed.length();
                            for (int j = 0; j < timed.length() && rowCount < 3; j++) {
                                JSONObject tb = timed.optJSONObject(j);
                                if (tb == null) continue;
                                String range = tb.optString("time_range_label", "日程").replace(" - ", "–");
                                String title = tb.optString("title", "").trim();
                                boolean done = tb.optBoolean("is_done", false);
                                if (!title.isEmpty()) {
                                    rows[rowCount++] = (done ? "✅ " : "⏰ ") + range + " " + title;
                                }
                            }
                        }

                        JSONArray allDay = todayObj.optJSONArray("all_day_tasks");
                        if (allDay != null) {
                            totalToday += allDay.length();
                            for (int j = 0; j < allDay.length() && rowCount < 3; j++) {
                                JSONObject ad = allDay.optJSONObject(j);
                                if (ad == null) continue;
                                String title = ad.optString("title", "").trim();
                                boolean done = ad.optBoolean("is_done", false);
                                if (!title.isEmpty()) {
                                    rows[rowCount++] = (done ? "✅ 全天 · " : "📌 全天 · ") + title;
                                }
                            }
                        }
                    }
                }
                parsedOk = true;
            } catch (Throwable ignored) {
            }
        }

        String holSuffix = "rest".equals(holidayStatus) ? " · 休" : ("work".equals(holidayStatus) ? " · 班" : "");
        String lunarLine = "";
        if (!lunarShort.isEmpty()) {
            lunarLine = "农历" + lunarShort;
            String extraTag = !festLabel.isEmpty() ? festLabel : solarTerm;
            if (!extraTag.isEmpty()) {
                lunarLine += " · " + extraTag;
            }
        } else if (!subLabel.isEmpty()) {
            lunarLine = subLabel;
        }
        String summaryText = !lunarLine.isEmpty()
                ? lunarLine
                : (focusMins > 0 ? ("日程 " + totalToday + " · 专注 " + focusMins + "m") : ("今日 " + totalToday + " 项日程"));
        String rightTitleText = focusMins > 0
                ? ("今日日程 (" + totalToday + " · 专注" + focusMins + "m)")
                : ("今日日程 (" + totalToday + " 项)");

        for (int i = 0; i < ids.length; i++) {
            RemoteViews rv = new RemoteViews(context.getPackageName(), R.layout.widget_calendar);
            rv.setOnClickPendingIntent(R.id.cal_widget_root, openCalPi);
            rv.setOnClickPendingIntent(R.id.cal_widget_open_btn, openCalPi);
            rv.setOnClickPendingIntent(R.id.cal_widget_refresh_btn, refreshPi);

            rv.setInt(
                    R.id.cal_widget_left_panel,
                    "setBackgroundResource",
                    isOffDay ? R.drawable.widget_calendar_rest_grad : R.drawable.widget_calendar_grad
            );
            rv.setTextViewText(R.id.cal_widget_month_wd, monthNum + "月 · " + wdLabel + holSuffix);
            rv.setTextViewText(R.id.cal_widget_day_num, String.valueOf(dayNum));
            rv.setTextViewText(R.id.cal_widget_summary, summaryText);
            rv.setTextViewText(R.id.cal_widget_right_title, rightTitleText);

            if (!parsedOk && (needsLogin || !hasToken)) {
                rv.setTextViewText(R.id.cal_widget_row_1, "🔐 轻点打开 有迹 登录同步日程");
                rv.setViewVisibility(R.id.cal_widget_row_2, View.GONE);
                rv.setViewVisibility(R.id.cal_widget_row_3, View.GONE);
            } else if (rowCount == 0) {
                rv.setTextViewText(R.id.cal_widget_row_1, parsedOk ? "🎉 今日暂无排期事项，轻点进入周时间轴排期" : "轻点打开应用同步今日日程");
                rv.setViewVisibility(R.id.cal_widget_row_2, View.GONE);
                rv.setViewVisibility(R.id.cal_widget_row_3, View.GONE);
            } else {
                rv.setTextViewText(R.id.cal_widget_row_1, rows[0]);
                if (rowCount >= 2) {
                    rv.setTextViewText(R.id.cal_widget_row_2, rows[1]);
                    rv.setViewVisibility(R.id.cal_widget_row_2, View.VISIBLE);
                } else {
                    rv.setViewVisibility(R.id.cal_widget_row_2, View.GONE);
                }
                if (rowCount >= 3) {
                    rv.setTextViewText(R.id.cal_widget_row_3, rows[2]);
                    rv.setViewVisibility(R.id.cal_widget_row_3, View.VISIBLE);
                } else {
                    rv.setViewVisibility(R.id.cal_widget_row_3, View.GONE);
                }
            }

            mgr.updateAppWidget(ids[i], rv);
        }
    }
}
