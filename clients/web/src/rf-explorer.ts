import type { Capabilities, DisplaySweep, Session, Sweep } from './types';

export const rfExplorerAdapter = 'rf-explorer-3g-serial';
export const surveyStepHz = 10_000;
export const surveyPoints = 112;
export const boardProfiles = {
  main: { label: 'Left SMA · WSUB1G', low: 240_000_000, high: 960_000_000 },
  expansion: { label: 'Right SMA · WSUB3G', low: 15_000_000, high: 2_700_000_000 },
};
export const rfExplorerAvailable = (features?: Capabilities['features']) =>
  features?.rf_explorer_3g_spectrum === true || features?.rf_explorer_survey_panorama === true;

export type RfRangeEntry = { start?: string; stop?: string; center?: string; span?: string };
export type RfBounds = { start_hz: number; stop_hz: number };

function integerHz(value?: string) {
  if (!value || !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))
    throw Error('Enter increasing frequencies in whole kHz');
  return Number(value);
}

export function rangeEndpoints(entry: RfRangeEntry): RfBounds {
  if (entry.center !== undefined || entry.span !== undefined) {
    const center = integerHz(entry.center),
      span = integerHz(entry.span);
    return { start_hz: center - span / 2, stop_hz: center + span / 2 };
  }
  return { start_hz: integerHz(entry.start), stop_hz: integerHz(entry.stop) };
}

export function validateRfRange(entry: RfRangeEntry, survey: boolean): RfBounds {
  const bounds = rangeEndpoints(entry);
  const { start_hz: low, stop_hz: high } = bounds;
  const span = high - low;
  if (![low, high].every((v) => Number.isSafeInteger(v) && v > 0 && v % 1000 === 0) || span <= 0)
    throw Error('Enter increasing frequencies in whole kHz');
  const outside = low < boardProfiles.expansion.low || high > boardProfiles.expansion.high;
  if (survey && (outside || span < (surveyPoints - 1) * surveyStepHz || span % surveyStepHz !== 0))
    throw Error(
      'Choose a range within 15–2700 MHz with a span of at least 1.11 MHz in 10 kHz steps',
    );
  if (!survey && (outside || span < 112_000 || span > 600_000_000))
    throw Error('Choose a window within 15–2700 MHz with a span of 112 kHz to 600 MHz');
  return bounds;
}

export function surveyCounts({ start_hz, stop_hz }: RfBounds) {
  const points = (stop_hz - start_hz) / surveyStepHz + 1;
  return { points, windows: Math.ceil(points / surveyPoints) };
}

export interface RfExplorerReadback {
  confirmed: boolean;
  active_board?: keyof typeof boardProfiles;
  active_input?: 'left' | 'right';
  board_min_hz?: number;
  board_max_hz?: number;
  board_max_span_hz?: number;
  points?: number;
  offset_db?: number;
  simulated?: boolean;
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
  const confirmed =
    settings.settings_readback === true && settings.settings_source === 'device_readback';
  const survey = surveyReadback(settings.survey);
  const board =
    settings.active_board === 'main' || settings.active_board === 'expansion'
      ? settings.active_board
      : undefined;
  const input = board === 'main' ? 'left' : 'right';
  const low = settings.board_min_hz,
    high = settings.board_max_hz;
  const validBoard =
    confirmed &&
    board !== undefined &&
    settings.active_input === input &&
    positiveInteger(low) &&
    positiveInteger(high) &&
    low >= boardProfiles[board].low &&
    high <= boardProfiles[board].high &&
    low < high;
  return {
    confirmed,
    active_board: validBoard ? board : undefined,
    active_input: validBoard ? input : undefined,
    board_min_hz: validBoard ? low : undefined,
    board_max_hz: validBoard ? high : undefined,
    board_max_span_hz:
      validBoard && positiveInteger(settings.board_max_span_hz)
        ? settings.board_max_span_hz
        : undefined,
    points: confirmed && positiveInteger(settings.points) ? settings.points : undefined,
    offset_db:
      typeof settings.offset_db === 'number' && Number.isFinite(settings.offset_db)
        ? settings.offset_db
        : undefined,
    simulated: typeof settings.simulated === 'boolean' ? settings.simulated : undefined,
    detector:
      typeof settings.detector === 'string' && settings.detector.length <= 64
        ? settings.detector
        : undefined,
    rbw_hz: positiveInteger(settings.rbw_hz) ? settings.rbw_hz : undefined,
    step_hz: positiveInteger(settings.step_hz) ? settings.step_hz : undefined,
    ...(survey && { survey }),
  };
}

export function latestRfReadback(
  sources: readonly (DisplaySweep | Sweep)[],
  sessionId: string,
  streamId: string,
  latest?: { at: string; readback: RfExplorerReadback },
) {
  for (const source of sources) {
    const at = source.interval.ended_at;
    if (
      source.session_id !== sessionId ||
      source.stream_id !== streamId ||
      (latest && at <= latest.at)
    )
      continue;
    const readback =
      'settings' in source ? rfExplorerReadback(source.settings) : source.rf_explorer;
    if (readback?.confirmed && readback.active_board) latest = { at, readback };
  }
  return latest;
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
  options: RfRangeEntry & { survey?: boolean } = {},
) {
  const serialPort = port.trim();
  if (!name.trim() || !serialPort || serialPort.includes('://') || serialPort.includes('\0'))
    throw Error('Enter a session name and a serial port');
  const custom = [options.start, options.stop, options.center, options.span].some(
    (v) => v !== undefined,
  );
  const bounds = custom ? validateRfRange(options, options.survey === true) : {};
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
