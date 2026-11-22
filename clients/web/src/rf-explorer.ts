import type { Session } from './types';

export const rfExplorerAdapter = 'rf-explorer-3g-serial';
const surveyStepHz = 10_000;

export interface RfExplorerReadback {
  detector?: string;
  rbw_hz?: number;
  step_hz?: number;
  survey?: {
    start_hz: number;
    stop_hz: number;
    step_hz: number;
    pass: number;
    tile: number;
    tiles: number;
    pass_id: string;
  };
}
function positiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}
function surveyReadback(value: unknown): RfExplorerReadback['survey'] {
  if (!value || typeof value !== 'object') return;
  const { start_hz, stop_hz, step_hz, pass, tile, tiles, pass_id } = value as Record<
    string,
    unknown
  >;
  if (
    positiveInteger(start_hz) &&
    positiveInteger(stop_hz) &&
    stop_hz > start_hz &&
    step_hz === surveyStepHz &&
    positiveInteger(pass) &&
    positiveInteger(tile) &&
    positiveInteger(tiles) &&
    tile <= tiles &&
    typeof pass_id === 'string' &&
    pass_id.length > 0 &&
    pass_id.length <= 200
  )
    return { start_hz, stop_hz, step_hz, pass, tile, tiles, pass_id };
}
export function rfExplorerReadback(
  settings?: Record<string, unknown>,
): RfExplorerReadback | undefined {
  if (settings?.adapter !== rfExplorerAdapter) return;
  const survey = surveyReadback(settings.survey);
  return {
    detector:
      typeof settings.detector === 'string' && settings.detector.length <= 64
        ? settings.detector
        : undefined,
    rbw_hz: positiveInteger(settings.rbw_hz) ? settings.rbw_hz : undefined,
    step_hz: positiveInteger(settings.step_hz) ? settings.step_hz : undefined,
    ...(survey && { survey }),
  };
}

export function standaloneRfExplorer(session: Session): boolean {
  const [stream] = session.streams;
  return (
    session.streams.length === 1 &&
    stream.role === 'spectrum_analyzer' &&
    stream.level_unit === 'dBm' &&
    stream.settings?.adapter === rfExplorerAdapter
  );
}

export function rfExplorerSession(
  name: string,
  port: string,
  options: { start?: string; stop?: string; survey?: boolean } = {},
) {
  const serialPort = port.trim();
  if (!name.trim() || !serialPort || serialPort.includes('://') || serialPort.includes('\0'))
    throw Error('Enter a session name and a serial port');
  const bounds: { start_hz?: number; stop_hz?: number } = {};
  if (options.start !== undefined || options.stop !== undefined) {
    const start = Number(options.start);
    const stop = Number(options.stop);
    if (
      ![start, stop].every((v) => Number.isSafeInteger(v) && v > 0 && v % 1000 === 0) ||
      stop <= start
    )
      throw Error('Enter increasing frequencies in whole kHz');
    if (
      options.survey &&
      (start < 15_000_000 ||
        stop > 2_700_000_000 ||
        stop - start < 111 * surveyStepHz ||
        (stop - start) % surveyStepHz !== 0)
    )
      throw Error(
        'Choose a range within 15–2700 MHz with a span of at least 1.11 MHz in 10 kHz steps',
      );
    bounds.start_hz = start;
    bounds.stop_hz = stop;
  }
  return {
    name: name.trim(),
    streams: [
      {
        device: 'RF Explorer 3G Combo',
        role: 'spectrum_analyzer',
        level_unit: 'dBm',
        port: serialPort,
        capabilities: ['sweep'],
        settings: {
          adapter: rfExplorerAdapter,
          baud: 500000,
          ...(options.survey && { survey_step_hz: surveyStepHz }),
          ...bounds,
        },
      },
    ],
    metadata: { receive_only: true, hardware_validation: 'not_run' },
  };
}
