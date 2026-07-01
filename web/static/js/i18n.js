/* ===== i18n Module =====
 * Depends on window._LOCALE (set by the template before this script).
 * Usage:  __('Hello')         → translated or key as fallback
 *         __('Hello {0}', x)   → positional replacement
 */

function __(key) {
  if (!window._LOCALE) return key;
  let s = window._LOCALE[key];
  if (s === undefined) s = key;
  // Positional replacement: __('Hello {0}', name)
  for (let i = 1; i < arguments.length; i++) {
    const re = new RegExp('\\{' + (i - 1) + '\\}', 'g');
    if (typeof s === 'string') {
      s = s.replace(re, String(arguments[i]));
    }
  }
  return s;
}
