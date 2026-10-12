/* 复制按钮:任何带 data-copy 的元素都能用。
 *
 * 管理页启用了严格 CSP(script-src 'self'),所以这里用事件委托而不是内联 onclick;
 * 片段被 HTMX 换掉之后也不需要重新绑定。
 *
 * 用法:
 *   <input id="fixed-feed" readonly value="https://...">
 *   <button type="button" class="button small" data-copy="#fixed-feed">复制</button>
 *   <button type="button" class="button small" data-copy-text="要复制的固定文本">复制</button>
 *
 * 复制成功后按钮文字临时变成「已复制」;失败变成「复制失败」(并在控制台留下原因)。
 */
(function () {
  'use strict';

  var RESET_DELAY = 1600;

  function textOf(trigger) {
    var literal = trigger.getAttribute('data-copy-text');
    if (literal !== null) return literal;
    var selector = trigger.getAttribute('data-copy');
    if (!selector) return '';
    var source = document.querySelector(selector);
    if (!source) return '';
    if (typeof source.value === 'string') return source.value;
    return (source.textContent || '').trim();
  }

  function legacyCopy(text) {
    // 老浏览器/非安全上下文兜底:建一个看不见的输入框选中后 execCommand
    var holder = document.createElement('textarea');
    holder.value = text;
    holder.setAttribute('readonly', 'readonly');
    holder.style.position = 'fixed';
    holder.style.top = '-1000px';
    holder.style.opacity = '0';
    document.body.appendChild(holder);
    holder.select();
    var ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(holder);
    return ok;
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return legacyCopy(text); }
      );
    }
    return Promise.resolve(legacyCopy(text));
  }

  function flash(trigger, ok) {
    if (!trigger.dataset.copyOriginal) {
      trigger.dataset.copyOriginal = trigger.textContent;
    }
    trigger.textContent = ok ? '已复制' : '复制失败';
    trigger.classList.toggle('copy-ok', ok);
    trigger.classList.toggle('copy-fail', !ok);
    window.clearTimeout(trigger._copyTimer);
    trigger._copyTimer = window.setTimeout(function () {
      trigger.textContent = trigger.dataset.copyOriginal;
      trigger.classList.remove('copy-ok', 'copy-fail');
    }, RESET_DELAY);
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-copy], [data-copy-text]');
    if (!trigger) return;
    event.preventDefault();
    var text = textOf(trigger);
    if (!text) {
      flash(trigger, false);
      return;
    }
    copy(text).then(function (ok) { flash(trigger, ok); });
  });

  // 点只读输入框就全选,方便手动 Ctrl+C(不用内联 onclick,CSP 下内联处理器会被拦)
  document.addEventListener('click', function (event) {
    var field = event.target;
    if (field && field.tagName === 'INPUT' && field.readOnly) {
      field.select();
    }
  });
})();
