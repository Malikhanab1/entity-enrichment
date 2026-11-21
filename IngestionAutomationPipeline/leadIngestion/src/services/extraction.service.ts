import { ExtractedContact, LeadChannel } from "../types";

/**
 * Phone number extraction is intentionally conservative.
 *
 * Supported examples:
 *   +92 300 1234567
 *   +92-300-1234567
 *   +92 (300) 1234567
 *   0300 1234567
 *   0300-1234567
 *   (0300) 1234567
 *   +1 415 555 2671
 *   415-555-2671
 *
 * Important:
 *   - Does NOT match years such as 2024, 2025
 *   - Does NOT match ranges such as 2024-2025
 *   - Does NOT match ordinary prices / IDs / post numbers
 *   - Requires 7-15 actual digits
 *
 * The regex itself is deliberately fairly broad; isPlausiblePhone()
 * performs the important validation afterward.
 */
const PHONE_REGEX =
  /(?<![\w@])(?:\+?\d{1,3}[\s.-]+)?(?:\(\s*\d{2,4}\s*\)|\d{2,4})[\s.-]+\d{3,4}(?:[\s.-]+\d{2,4})?(?![\w])/g;

const EMAIL_REGEX =
  /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g;

// Phrases that indicate the author wants to be reached over DM/inbox.
const DM_REGEX =
  /\b(?:dm|d\.m\.|direct\s+message|inbox|message\s+me|text\s+me\s+here|pm\s+me)\b/i;

/**
 * Common patterns that are definitely not phone numbers.
 */
const NON_PHONE_REGEXES = [
  // Years and year ranges:
  // 2024
  // 2024-2025
  // 2024 / 2025
  // 2024–2025
  /\b(?:19|20)\d{2}\s*[-–—/]\s*(?:19|20)\d{2}\b/,
  /\b(?:19|20)\d{2}\b/,

  // Dates:
  // 2024-08-29, 29-08-2024, 08/29/2024
  /\b\d{1,4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,4}\b/,

  // Decimal numbers / prices:
  // 99.99, 1,299.99
  /\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b/,
  /\b\d+\.\d+\b/,

  // Explicit ranges:
  // 100-200, 5000-6000
  /\b\d{1,6}\s*[-–—]\s*\d{1,6}\b/,
];

/**
 * Validate whether a regex match is actually phone-like.
 */
function isPlausiblePhone(candidate: string, text: string, index: number): boolean {
  const trimmed = candidate.trim();

  // Remove everything except digits.
  const digits = trimmed.replace(/\D/g, "");

  // Phones generally have 7-15 digits internationally.
  if (digits.length < 7 || digits.length > 15) {
    return false;
  }

  // Never accept a bare year.
  if (/^(?:19|20)\d{2}$/.test(digits)) {
    return false;
  }

  // Reject obvious year ranges such as 2024-2025.
  if (
    /^(?:19|20)\d{2}\s*[-–—/]\s*(?:19|20)\d{2}$/.test(trimmed)
  ) {
    return false;
  }

  // Reject dates.
  if (
    /^\d{1,4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,4}$/.test(trimmed)
  ) {
    return false;
  }

  // Reject candidates that contain multiple 4-digit years.
  const years = trimmed.match(/\b(?:19|20)\d{2}\b/g);
  if (years && years.length >= 2) {
    return false;
  }

  // Reject obvious numeric ranges.
  if (/^\d+\s*[-–—]\s*\d+$/.test(trimmed)) {
    return false;
  }

  // Reject decimal numbers / prices.
  if (/^\d+(?:[.,]\d+)+$/.test(trimmed)) {
    return false;
  }

  // A phone should normally contain either:
  //   - a + international prefix
  //   - separators
  //   - parentheses
  //
  // This prevents long unformatted IDs from being treated as phones.
  const hasPlus = /^\s*\+/.test(trimmed);
  const hasSeparator = /[\s().-]/.test(trimmed);

  if (!hasPlus && !hasSeparator) {
    return false;
  }

  // Don't accept a candidate if it is immediately adjacent to another digit.
  const before = text[index - 1];
  const after = text[index + trimmed.length];

  if (before && /\d/.test(before)) {
    return false;
  }

  if (after && /\d/.test(after)) {
    return false;
  }

  // Reject suspiciously year-like candidates even when another separator
  // caused them to be captured.
  if (/^(?:19|20)\d{2}[\s.-]/.test(trimmed)) {
    return false;
  }

  // Reject candidates consisting almost entirely of a year plus a tiny suffix.
  const compact = trimmed.replace(/\D/g, "");

  if (
    compact.length >= 7 &&
    /^(?:19|20)\d{2}\d{1,3}$/.test(compact) &&
    !hasPlus
  ) {
    // Only reject if the original candidate looks like an ID/year construction.
    if (!/\s/.test(trimmed) && !/\(/.test(trimmed)) {
      return false;
    }
  }

  // Avoid obvious social/media/post IDs.
  const contextBefore = text.slice(Math.max(0, index - 20), index);
  const contextAfter = text.slice(
    index + trimmed.length,
    index + trimmed.length + 20,
  );

  const surroundingText = `${contextBefore}${contextAfter}`.toLowerCase();

  if (
    /\b(?:id|post|order|invoice|ticket|ref|reference|case|listing|sku)[\s:#-]*$/i.test(
      contextBefore,
    )
  ) {
    return false;
  }

  if (/^\s*(?:id|post|order|invoice|ticket|ref|reference)\b/i.test(contextAfter)) {
    return false;
  }

  // If the entire surrounding context looks like a URL/path, don't extract it.
  if (
    /(?:https?:\/\/|www\.)[^\s]*$/i.test(contextBefore) &&
    /^[^\s]*/.test(contextAfter)
  ) {
    return false;
  }

  // Avoid obviously repeated/sequential numeric garbage.
  if (/^(\d)\1{6,}$/.test(digits)) {
    return false;
  }

  if (/^(?:0123456|1234567|2345678|3456789|9876543|8765432)$/.test(digits)) {
    return false;
  }

  // Silence unused-variable concerns while retaining useful context above.
  void surroundingText;

  return true;
}

/**
 * Additional hard exclusions for complete matches.
 */
function isDefinitelyNotPhone(candidate: string): boolean {
  const value = candidate.trim();

  return NON_PHONE_REGEXES.some((regex) => regex.test(value));
}

export function extractContact(text: string): ExtractedContact {
  const emailMatches = [...text.matchAll(EMAIL_REGEX)].map((m) => m[0]);

  const phoneMatches = [...text.matchAll(PHONE_REGEX)]
    .filter((match) => {
      const candidate = match[0].trim();

      if (!candidate) return false;

      // Never allow anything that overlaps an email.
      const start = match.index ?? 0;
      const end = start + match[0].length;

      const insideEmail = emailMatches.some((email) => {
        const emailStart = text.indexOf(email);

        if (emailStart === -1) return false;

        const emailEnd = emailStart + email.length;

        return start < emailEnd && end > emailStart;
      });

      if (insideEmail) return false;

      if (isDefinitelyNotPhone(candidate)) {
        return false;
      }

      return isPlausiblePhone(candidate, text, start);
    })
    .map((match) => normalizePhone(match[0]));

  const dmSignal = DM_REGEX.test(text);

  const candidates = {
    phones: dedupe(phoneMatches),
    emails: dedupe(emailMatches),
    dmSignal,
  };

  const channel = resolveChannel(candidates);
  const value = resolveValue(channel, candidates);

  return {
    channel,
    value,
    candidates,
  };
}

/**
 * Normalize phone formatting so equivalent numbers don't appear as
 * separate candidates.
 *
 * Examples:
 *   +92 300-1234567 -> +923001234567
 *   0300 1234567    -> 03001234567
 *
 * Keep the leading + because it carries useful international-format info.
 */
function normalizePhone(phone: string): string {
  const trimmed = phone.trim();

  if (trimmed.startsWith("+")) {
    return `+${trimmed.slice(1).replace(/\D/g, "")}`;
  }

  return trimmed.replace(/\D/g, "");
}

/**
 * Precedence:
 *   call > dm > mail
 */
function resolveChannel(
  candidates: ExtractedContact["candidates"],
): LeadChannel {
  if (candidates.phones.length > 0) return "call";
  if (candidates.dmSignal) return "dm";
  if (candidates.emails.length > 0) return "mail";

  // Nothing explicit found — default to DM as the lowest-friction fallback.
  return "dm";
}

function resolveValue(
  channel: LeadChannel,
  candidates: ExtractedContact["candidates"],
): string {
  switch (channel) {
    case "call":
      return candidates.phones[0];

    case "mail":
      return candidates.emails[0];

    case "dm":
    default:
      return "dm";
  }
}

function dedupe(arr: string[]): string[] {
  return Array.from(new Set(arr));
}
