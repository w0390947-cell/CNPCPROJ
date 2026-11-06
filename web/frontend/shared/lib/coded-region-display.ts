/** Place-name aliases for the two customer-facing example pages only. */
const aliases: Record<string, string> = {
  山城: 'SC',
  化子坪: 'YA_B',
  榆树: 'YA_C',
  shancheng: 'SC',
  huaziping: 'YA_B',
  yushu: 'YA_C',
  延安: '区域',
};
const placeNames = /山城|化子坪|榆树|shancheng|huaziping|yushu|延安/gi;

export function codedRegionText(text: string): string {
  return text.replace(placeNames, (name) => aliases[name.toLowerCase()]);
}

/**
 * Read-only presentation projection. Never use for requests, record validation,
 * hashes, persistence or exports. All unchanged branches retain their identity.
 * Map names and references together so network/device display axes still agree.
 */
export function codedRegionDisplay<T>(value: T): T {
  if (typeof value === 'string') return codedRegionText(value) as T;
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) {
    const next = value.map((item) => codedRegionDisplay(item));
    return (next.some((item, i) => item !== value[i]) ? next : value) as T;
  }
  let changed = false;
  const entries = Object.entries(value).map(([key, item]) => {
    const nextKey = codedRegionText(key);
    // The embedded input file is an opaque replay artifact, not display text.
    const next =
      key === 'input_snapshot_json' ? item : codedRegionDisplay(item);
    changed ||= nextKey !== key || next !== item;
    return [nextKey, next];
  });
  return changed ? (Object.fromEntries(entries) as T) : value;
}
