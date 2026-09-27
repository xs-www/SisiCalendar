/* 日历视图:月 / 周 / 日 / 列表,支持筛选与拖动确认。 */
(function () {
  "use strict";

  const host = document.getElementById("calendar");
  if (!host || typeof FullCalendar === "undefined") {
    return;
  }

  const api = host.dataset.api || "/api/v1";
  const csrf = host.dataset.csrf || "";
  const filters = { q: "", status: "", kind: "", recurring: "", source: "" };

  // ---------- 提示 ----------
  function toast(message, kind) {
    const box = document.getElementById("toast");
    if (!box || !message) return;
    box.textContent = message;
    box.className = "toast " + (kind || "ok");
    box.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () {
      box.hidden = true;
    }, 4200);
  }

  function flashFromStorage() {
    try {
      const message = window.sessionStorage.getItem("sisi_flash");
      const kind = window.sessionStorage.getItem("sisi_flash_kind") || "ok";
      if (message) {
        toast(message, kind);
        window.sessionStorage.removeItem("sisi_flash");
        window.sessionStorage.removeItem("sisi_flash_kind");
      }
    } catch (err) {
      /* 隐私模式下忽略 */
    }
  }
  flashFromStorage();

  // ---------- 数据源 ----------
  function buildUrl(info) {
    const params = new URLSearchParams();
    params.set("start", info.startStr);
    params.set("end", info.endStr);
    params.set("expand", "true");
    if (filters.q) params.set("q", filters.q);
    if (filters.status) params.set("status", filters.status);
    if (filters.kind === "all_day") params.set("all_day", "true");
    if (filters.kind === "timed") params.set("all_day", "false");
    if (filters.recurring === "1") params.set("recurring", "true");
    if (filters.recurring === "0") params.set("recurring", "false");
    if (filters.source) params.set("source", filters.source);
    return api + "/events?" + params.toString();
  }

  function toCalendarEvent(item) {
    const recurring = Boolean(item.recurring);
    return {
      id: item.id,
      title: item.title,
      start: item.start,
      end: item.end,
      allDay: Boolean(item.allDay),
      editable: !recurring,
      classNames: item.status === "cancelled" ? ["ev-cancelled"] : [],
      extendedProps: {
        event_id: item.event_id || item.id,
        recurring: recurring,
        timezone: item.timezone || "",
        location: item.location || "",
        description: item.description || "",
        rrule_label: item.rrule_label || "",
        status: item.status || "confirmed",
        source: item.source || "web",
        updated_at: item.updated_at || ""
      }
    };
  }

  async function loadEvents(info, success, failure) {
    try {
      const response = await fetch(buildUrl(info), {
        headers: { Accept: "application/json" },
        credentials: "same-origin"
      });
      if (response.status === 401) {
        window.location.href = "/login?next=/admin";
        return;
      }
      if (!response.ok) {
        throw new Error("加载事件失败(" + response.status + ")");
      }
      const payload = await response.json();
      success((payload.items || []).map(toCalendarEvent));
    } catch (err) {
      toast(err.message || "加载事件失败", "error");
      failure(err);
    }
  }

  // ---------- 拖动修改 ----------
  async function patchEvent(eventId, body) {
    const response = await fetch(api + "/events/" + encodeURIComponent(eventId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      credentials: "same-origin",
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(function () {
      return {};
    });
    if (!response.ok) {
      const message = (payload.error && payload.error.message) || "修改失败";
      throw new Error(message);
    }
    return payload;
  }

  async function handleMove(info) {
    const event = info.event;
    const props = event.extendedProps || {};
    if (props.recurring) {
      info.revert();
      toast("重复事件的单个实例暂不支持拖动修改,请编辑原始日程。", "error");
      return;
    }

    const label = event.allDay
      ? event.startStr + " 起"
      : event.start.toLocaleString() + " 起";
    const confirmed = window.confirm(
      "确认把“" + event.title + "”改到 " + label + " 吗?"
    );
    if (!confirmed) {
      info.revert();
      return;
    }

    const body = { expected_updated_at: props.updated_at || undefined };
    if (event.allDay) {
      body.all_day = true;
      body.start_date = event.startStr;
      body.end_date = event.endStr || null;
    } else {
      body.all_day = false;
      body.start_at = event.start.toISOString();
      body.end_at = (event.end || event.start).toISOString();
    }
    if (props.timezone) body.timezone = props.timezone;

    try {
      await patchEvent(props.event_id, body);
      toast("日程已更新并同步", "ok");
      calendar.refetchEvents();
    } catch (err) {
      info.revert();
      toast(err.message, "error");
    }
  }

  // ---------- 初始化 ----------
  const calendar = new FullCalendar.Calendar(host, {
    locale: "zh-cn",
    initialView: "dayGridMonth",
    height: "auto",
    firstDay: 1,
    nowIndicator: true,
    selectable: true,
    selectMirror: true,
    dayMaxEvents: 4,
    headerToolbar: {
      left: "prev,next today",
      center: "title",
      right: "dayGridMonth,timeGridWeek,timeGridDay,listMonth"
    },
    buttonText: {
      today: "今天",
      month: "月",
      week: "周",
      day: "日",
      list: "列表"
    },
    events: loadEvents,
    select: function (info) {
      const params = new URLSearchParams();
      params.set("start", info.startStr);
      params.set("end", info.endStr);
      params.set("all_day", info.allDay ? "1" : "0");
      window.location.href = "/admin/events/new?" + params.toString();
    },
    eventClick: function (info) {
      const props = info.event.extendedProps || {};
      if (props.recurring && !window.confirm("这是重复事件的一个实例,将打开整个系列进行编辑,继续?")) {
        return;
      }
      window.location.href = "/admin/events/" + encodeURIComponent(props.event_id);
    },
    eventDrop: handleMove,
    eventResize: handleMove,
    eventDidMount: function (info) {
      const props = info.event.extendedProps || {};
      const bits = [];
      if (props.timezone) bits.push("时区:" + props.timezone);
      if (props.location) bits.push("地点:" + props.location);
      if (props.rrule_label) bits.push("重复:" + props.rrule_label);
      if (props.description) bits.push(props.description.slice(0, 160));
      if (bits.length) info.el.title = bits.join("\n");
    }
  });
  calendar.render();

  // ---------- 筛选 ----------
  function readFilters() {
    filters.q = (document.getElementById("filter-q").value || "").trim();
    filters.status = document.getElementById("filter-status").value || "";
    filters.kind = document.getElementById("filter-kind").value || "";
    filters.recurring = document.getElementById("filter-recurring").value || "";
    filters.source = document.getElementById("filter-source").value || "";
  }

  document.getElementById("filter-apply").addEventListener("click", function () {
    readFilters();
    calendar.refetchEvents();
  });

  document.getElementById("filter-reset").addEventListener("click", function () {
    document.getElementById("filter-q").value = "";
    document.getElementById("filter-status").value = "";
    document.getElementById("filter-kind").value = "";
    document.getElementById("filter-recurring").value = "";
    document.getElementById("filter-source").value = "";
    readFilters();
    calendar.refetchEvents();
  });

  document.getElementById("filter-q").addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      readFilters();
      calendar.refetchEvents();
    }
  });
})();
