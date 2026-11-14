import { expect, it } from 'vitest';
import { spectrumColors } from '../src/spectrum-renderer';
function luminance(hex: string) {
  const values = [1, 3, 5].map((i) => {
    const value = Number.parseInt(hex.slice(i, i + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
}
it('keeps essential spectrum and baseline strokes above the AA non-text contrast threshold in both themes', () => {
  for (const [background, signal, warn] of [
    ['#ffffff', '#007b68', '#805500'],
    ['#171e27', '#66dbc1', '#f4c66a'],
  ]) {
    const palette = spectrumColors({
      getPropertyValue: (name: string) =>
        name === '--signal' ? signal : name === '--warn' ? warn : '#303d4b',
    });
    for (const color of [palette.signal, palette.baseline95, palette.baseline50]) {
      const values = [luminance(color), luminance(background)].sort((a, b) => a - b);
      expect(
        (values[1] + 0.05) / (values[0] + 0.05),
        color + ' on ' + background,
      ).toBeGreaterThanOrEqual(3);
    }
  }
});
