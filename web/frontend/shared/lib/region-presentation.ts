/** Stable presentation names for the synthetic regions used across Web features. */
export const REGION_DEFINITIONS = [
  {
    id: 'SC',
    displayName: '山城参数化微网（SC）',
  },
  {
    id: 'YA_B',
    displayName: '延安合成微网 B（YA_B）',
  },
  {
    id: 'YA_C',
    displayName: '延安合成微网 C（YA_C）',
  },
] as const;

export type RegionDefinition = (typeof REGION_DEFINITIONS)[number];
export type RegionCode = RegionDefinition['id'];

/** Returns the raw code when a newer backend region is not registered yet. */
export function regionDisplayName(code: string): string {
  return (
    REGION_DEFINITIONS.find((region) => region.id === code)?.displayName ?? code
  );
}
