/* 日历视图:多日历着色 + 侧栏勾选,支持筛选与拖动确认。 */
(function () {
  "use strict";

  const host = document.getElementById("calendar");
  if (!host || typeof FullCalendar === "undefined") {
    return;
  }

  // 公开前缀:从 `/calendar/...` 进来的页面要回到 `/calendar/...`,从裸路径进来则不加前缀
  const basePath = (document.body && document.body.dataset.basePath) || "";
  const api = host.dataset.api || basePath + "/api/v1";
  const csrf = host.dataset.csrf || "";
  const filters = { q: "", status: "", kind: "", recurring: "", source: "" };

  // ---------- 日历表(由模板通过 data-calendars 传入) ----------
  function parseCalendarOptions() {
    const raw = host.dataset.calendars || "[]";
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }

  const calendarOptions = parseCalendarOptions();
  const calendarById = {};
  calendarOptions.forEach(function (item) {
    if (item && item.id) {
      calendarById[item.id] = item;
    }
  });

  function filterBoxes() {
    return Array.prototype.slice.call(
      document.querySelectorAll('input[name="calendar-filter"]')
    );
  }

  /* 勾选中的日历 id;没有任何勾选框时视为全部可见日历。 */
  function selectedCalendarIds() {
    const boxes = filterBoxes();
    if (!boxes.length) {
      return [];
    }
    return boxes
      .filter(function (box) {
        return box.checked;
      })
      .map(function (box) {
        return box.dataset.calendarId || box.value;
      })
      .filter(Boolean);
  }

  function hasSelection() {
    const boxes = filterBoxes();
    if (!boxes.length) {
      return true;
    }
    return boxes.some(function (box) {
      return box.checked;
    });
  }

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
    selectedCalendarIds().forEach(function (id) {
      params.append("calendar_id", id);
    });
    if (filters.q) params.set("q", filters.q);
    if (filters.status) params.set("status", filters.status);
    if (filters.kind === "all_day") params.set("all_day", "true");
    if (filters.kind === "timed") params.set("all_day", "false");
    if (filters.recurring === "1") params.set("recurring", "true");
    if (filters.recurring === "0") params.set("recurring", "false");
    if (filters.source) params.set("source", filters.source);
    return api + "/events?" + params.toString();
  }

  function colorOf(item) {
    if (item.calendar_color) {
      return item.calendar_color;
    }
    const option = calendarById[item.calendar_id];
    return option && option.color ? option.color : null;
  }

  function toCalendarEvent(item) {
    const recurring = Boolean(item.recurring);
    // 只读日历(或重复事件的实例)不可拖动改期
    const writable = item.can_write !== false && !recurring;
    const color = colorOf(item);
    const event = {
      id: item.id,
      title: item.title,
      start: item.start,
      end: item.end,
      allDay: Boolean(item.allDay),
      editable: writable,
      classNames: item.status === "cancelled" ? ["ev-cancelled"] : [],
      extendedProps: {
        event_id: item.event_id || item.id,
        recurring: recurring,
        timezone: item.timezone || "",
        location: item.location || "",
        description: item.description || "",
        url: item.url || "",
        reminder_labels: item.reminder_labels || [],
        rrule_label: item.rrule_label || "",
        status: item.status || "confirmed",
        source: item.source || "web",
        updated_at: item.updated_at || "",
        calendar_id: item.calendar_id || "",
        calendar_name: item.calendar_name || "",
        can_write: item.can_write !== false
      }
    };
    if (color) {
      event.backgroundColor = color;
      event.borderColor = color;
    }
    return event;
  }

  async function loadEvents(info, success, failure) {
    // 一个日历都没勾选时不打扰后端,直接显示空日历
    if (!hasSelection()) {
      success([]);
      return;
    }
    try {
      const response = await fetch(buildUrl(info), {
        headers: { Accept: "application/json" },
        credentials: "same-origin"
      });
      if (response.status === 401) {
        window.location.href = basePath + "/login?next=" + encodeURIComponent(basePath + "/admin");
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
    if (props.can_write === false) {
      info.revert();
      toast("你没有修改该事件所在日历的权限。", "error");
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
      window.location.href = basePath + "/admin/events/new?" + params.toString();
    },
    eventClick: function (info) {
      const props = info.event.extendedProps || {};
      if (props.recurring && !window.confirm("这是重复事件的一个实例,将打开整个系列进行编辑,继续?")) {
        return;
      }
      window.location.href = basePath + "/admin/events/" + encodeURIComponent(props.event_id);
    },
    eventDrop: handleMove,
    eventResize: handleMove,
    eventDidMount: function (info) {
      const props = info.event.extendedProps || {};
      const bits = [];
      if (props.calendar_name) bits.push("日历:" + props.calendar_name);
      if (props.timezone) bits.push("时区:" + props.timezone);
      if (props.location) bits.push("地点:" + props.location);
      if (props.rrule_label) bits.push("重复:" + props.rrule_label);
      if (props.reminder_labels && props.reminder_labels.length) {
        bits.push("提醒:" + props.reminder_labels.join(" / "));
      }
      if (props.url) bits.push("链接:" + props.url);
      if (props.description) bits.push(props.description.slice(0, 160));
      if (bits.length) info.el.title = bits.join("\n");
    }
  });
  calendar.render();

  // ---------- 侧栏:日历勾选 ----------
  filterBoxes().forEach(function (box) {
    box.addEventListener("change", function () {
      if (!hasSelection()) {
        toast("至少勾选一个日历;当前不显示任何日程。", "error");
      }
      calendar.refetchEvents();
    });
  });

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