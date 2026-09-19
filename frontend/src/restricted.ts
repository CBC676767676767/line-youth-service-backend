/**
 * Browser copy of the published exclusions, so the form can say no before an
 * applicant fills everything in. The server refuses the submission regardless;
 * this only moves the message earlier. Matching mirrors app/restricted.py.
 */
import { api } from "./shared/api";

export type RestrictedEntry = {
  id: string;
  name: string;
  kind: string;
  clause: string;
  distinctive: boolean;
  aliases: string[];
};

export type RestrictedCatalog = {
  version: string;
  source: { title: string; url: string; checked_date: string };
  notice: string;
  entries: RestrictedEntry[];
};

const LATIN = /[a-z0-9]/;

function normalize(value: string): string {
  return (value || "").normalize("NFKC").toLowerCase();
}

function aliases(entry: RestrictedEntry): string[] {
  // Taken from the server so both sides read a receipt the same way. Deriving
  // them here instead would silently miss names the server still refuses.
  return [entry.name, ...(entry.aliases || [])].filter(Boolean);
}

function hit(text: string, alias: string): boolean {
  const needle = normalize(alias);
  if (!needle) return false;
  const haystack = normalize(text);
  if (!LATIN.test(needle)) return haystack.includes(needle);
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?<![a-z0-9])${escaped}(?![a-z0-9])`).test(haystack);
}

/** The applicant typed this, so even a common word is deliberate here. */
export function matchDeclared(
  value: string,
  catalog: RestrictedCatalog | null,
): RestrictedEntry | null {
  if (!catalog || !value.trim()) return null;
  return (
    catalog.entries.find((entry) =>
      aliases(entry).some((alias) => hit(value, alias)),
    ) || null
  );
}

/** Receipt prose. Only names distinctive enough to rely on are reported. */
export function scanText(
  value: string,
  catalog: RestrictedCatalog | null,
): RestrictedEntry[] {
  if (!catalog || !value.trim()) return [];
  return catalog.entries.filter(
    (entry) =>
      entry.distinctive && aliases(entry).some((alias) => hit(value, alias)),
  );
}

let cached: Promise<RestrictedCatalog | null> | null = null;

export function loadRestricted(): Promise<RestrictedCatalog | null> {
  // A failed load must not block an application: the server still refuses one.
  if (!cached)
    cached = api<RestrictedCatalog>("/precheck/restricted-tools").catch(
      () => null,
    );
  return cached;
}
