/**
 * license-format.js — Single source of truth for Ghost license key format
 * =========================================================================
 * Canonical format:  GHOST-XXXX-XXXX-XXXX-XXXX
 *   • Prefix:  GHOST-
 *   • 4 groups of exactly 4 alphanumeric characters (A-Z, 0-9)
 *   • Groups separated by hyphens
 *
 * Import/use in browser globals:
 *   <script src="assets/js/license-format.js"></script>
 *   LicenseFormat.REGEX.test(key)
 *
 * Import/use in Node (CommonJS):
 *   const { GHOST_KEY_RE, normalizeLicenseKey, isValidLicenseFormat } = require('./assets/js/license-format');
 */

(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    // CommonJS (Node.js)
    module.exports = factory();
  } else {
    // Browser global
    root.LicenseFormat = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /**
   * Canonical Ghost license key regex.
   * Matches: GHOST-XXXX-XXXX-XXXX-XXXX
   *   where each X is [A-Z0-9] (case-insensitive via flag).
   *
   * This is the ONLY regex that should be used for format validation across
   * the entire project — frontend and backend alike.
   */
  const GHOST_KEY_RE = /^GHOST-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/i;

  /**
   * Normalise a raw license key input:
   *   1. Trim leading/trailing whitespace
   *   2. Convert to uppercase
   *
   * Does NOT alter the value in any other way (no prefix insertion, no truncation).
   *
   * @param {string} raw
   * @returns {string}
   */
  function normalizeLicenseKey(raw) {
    return (raw || '').trim().toUpperCase();
  }

  /**
   * Return true if the key matches the canonical format after normalisation.
   * This is a FORMAT-ONLY check — it does NOT verify against Redis.
   * Redis is the final source of truth for whether a key is valid/available.
   *
   * @param {string} key
   * @returns {boolean}
   */
  function isValidLicenseFormat(key) {
    return GHOST_KEY_RE.test(normalizeLicenseKey(key));
  }

  return {
    /** @type {RegExp} Canonical Ghost key regex — /^GHOST-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/i */
    REGEX: GHOST_KEY_RE,
    /** Alias kept for Node/server.js consumers */
    GHOST_KEY_RE,
    normalizeLicenseKey,
    isValidLicenseFormat,
  };
}));
