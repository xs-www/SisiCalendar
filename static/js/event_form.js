/* 新建 / 编辑日程表单:提交到 JSON API,并显示字段级错误。 */
(function () {
  "use strict";

  const form = document.getElementById("event-form");
  if (!form) {
    return;
  }

  // 公开前缀:从 `/calendar/...` 进来的页面要回到 `/calendar/...`,从裸路径进来则不加前缀
  const basePath = (document.body && document.body.dataset.basePath) || "";
  const api = form.dataset.api || basePath + "/api/v1";
  const csrf = form.dataset.csrf || "";
  const mode = form.dataset.mode || "create";
  const eventId = form.dataset.eventId || "";
  const redirect = form.dataset.redirect || basePath + "/admin";

  const allDayBox = document.getElementById("all-day");
  const timedFields = document.getElementById("timed-fields");
  const allDayFields = document.getElementById("allday-fields");
  const errorBox = document.getElementById("form-errors");

  function syncVisibility() {
    const allDay = allDayBox.checked;
    timedFields.hidden = allDay;
    allDayFields.hidden = !allDay;
  }
  allDayBox.addEventListener("change", syncVisibility);
  syncVisibility();

  function showErrors(message, fields) {
    const parts = [];
    if (message) parts.push(message);
    if (fields) {
      Object.keys(fields).forEach(function (key) {
        parts.push("• " + key + ":" + fields[key]);
      });
    }
    errorBox.textContent = parts.join("\n");
    errorBox.hidden = parts.length === 0;
    if (parts.length) {
      errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function flash(message, kind) {
    try {
      window.sessionStorage.setItem("sisi_flash", message);
      window.sessionStorage.setItem("sisi_flash_kind", kind || "ok");
    } catch (err) {
      /* 忽略 */
    }
  }

  /* 收集提醒:预设勾选 + 自定义分钟数(逗号分隔),去重后从大到小。 */
  function collectReminders() {
    const values = [];
    form.querySelectorAll("input[name=reminder_preset]:checked").forEach(function (box) {
      const value = parseInt(box.value, 10);
      if (!Number.isNaN(value)) {
        values.push(value);
      }
    });
    const custom = document.getElementById("reminder-custom");
    if (custom) {
      (custom.value || "").split(/[,;，；\s]+/).forEach(function (chunk) {
        if (!chunk) return;
        const value = parseInt(chunk, 10);
        if (!Number.isNaN(value)) {
          values.push(value);
        }
      });
    }
    return Array.from(new Set(values)).sort(function (a, b) {
      return b - a;
    });
  }

  function buildPayload(data) {
    const payload = {
      title: (data.get("title") || "").toString().trim(),
      description: (data.get("description") || "").toString().trim() || null,
      location: (data.get("location") || "").toString().trim() || null,
      url: (data.get("url") || "").toString().trim() || null,
      reminders: collectReminders(),
      all_day: allDayBox.checked,
      timezone: (data.get("timezone") || "").toString().trim(),
      status: data.get("status") || "confirmed",
      recurrence: data.get("recurrence") || "none",
      recurrence_until: (data.get("recurrence_until") || "").toString() || null
    };

    if (allDayBox.checked) {
      payload.start_date = (data.get("start_date") || "").toString() || null;
      payload.end_date = (data.get("end_date") || "").toString() || null;
    } else {
      payload.start_at = (data.get("start_at") || "").toString() || null;
      payload.end_at = (data.get("end_at") || "").toString() || null;
    }

    const expected = (data.get("expected_updated_at") || "").toString();
    if (expected) {
      payload.expected_updated_at = expected;
    }

    // 日历归属:新建时决定落库日历,编辑时用于迁移;只读事件没有这个下拉框
    const calendarId = (data.get("calendar_id") || "").toString().trim();
    if (calendarId) {
      payload.calendar_id = calendarId;
    }
    return payload;
  }

  function describeSync(sync) {
    if (!sync) return "日程已保存";
    if (sync.ok === false) {
      return "日程已保存,但 ICS 生成失败,请在“同步状态”页重试。";
    }
    const bits = ["日程已保存并同步"];
    if (sync.version) bits.push("v" + sync.version);
    if (sync.mail_created) bits.push("新增 " + sync.mail_created + " 个发信任务");
    return bits.join(" · ");
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    const submitButton = form.querySelector("button[type=submit]");
    if (submitButton && submitButton.disabled) {
      // 只读事件:按钮已停用,不该走到这里
      return;
    }
    submitButton.disabled = true;
    errorBox.hidden = true;

    const payload = buildPayload(new FormData(form));
    const url = mode === "edit" ? api + "/events/" + encodeURIComponent(eventId) : api + "/events";
    const method = mode === "edit" ? "PATCH" : "POST";

    try {
      const response = await fetch(url, {
        method: method,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        credentials: "same-origin",
        body: JSON.stringify(payload)
      });
      const body = await response.json().catch(function () {
        return {};
      });

      if (!response.ok) {
        const detail = body.error || {};
        showErrors(detail.message || "保存失败(" + response.status + ")", detail.fields);
        submitButton.disabled = false;
        return;
      }

      const sync = body.sync;
      flash(describeSync(sync), sync && sync.ok === false ? "error" : "ok");
      window.location.href = redirect;
    } catch (err) {
      showErrors(err.message || "网络错误,请重试");
      submitButton.disabled = false;
    }
  });

  const deleteButton = document.getElementById("delete-event");
  if (deleteButton) {
    const calendarSelect = document.getElementById("calendar-id");
    const calendarName = calendarSelect && calendarSelect.selectedOptions.length
      ? calendarSelect.selectedOptions[0].textContent.trim()
      : "所属日历";
    deleteButton.addEventListener("click", async function () {
      if (!window.confirm(
        "确定删除这条日程吗?删除后会立即从「" + calendarName + "」的订阅日历中移除,并把最新快照发给对应的收件人。"
      )) {
        return;
      }
      deleteButton.disabled = true;
      try {
        const response = await fetch(api + "/events/" + encodeURIComponent(eventId), {
          method: "DELETE",
          headers: { "X-CSRF-Token": csrf },
          credentials: "same-origin"
        });
        const body = await response.json().catch(function () {
          return {};
        });
        if (!response.ok) {
          const detail = body.error || {};
          showErrors(detail.message || "删除失败(" + response.status + ")", detail.fields);
          deleteButton.disabled = false;
          return;
        }
        flash("日程已删除并同步", "ok");
        window.location.href = redirect;
      } catch (err) {
        showErrors(err.message || "网络错误,请重试");
        deleteButton.disabled = false;
      }
    });
  }
})();
