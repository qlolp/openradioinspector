import { useEffect, useRef, useState } from 'react';
import { ResourceScope } from './api';
import { hz } from './components';
import { t } from './i18n';
import { formatQuantity } from './station';
import type { Desk } from './useDesk';
import { encodePathSegment } from './wire';

import type {
  PairedOriginals,
  PairedRecheckResult,
} from '../../../contracts/generated/typescript/runtime';

function fields(input: unknown, keys: string[]): input is Record<string, unknown> {
  return (
    !!input &&
    typeof input === 'object' &&
    !Array.isArray(input) &&
    Object.keys(input).length === keys.length &&
    keys.every((key) => Object.hasOwn(input, key))
  );
}
const sourceId = (value: unknown): value is string =>
  typeof value === 'string' && value.trim().length > 0;
function pair(input: unknown): input is PairedOriginals {
  return (
    fields(input, ['sweep_id', 'observation_ids']) &&
    sourceId(input.sweep_id) &&
    Array.isArray(input.observation_ids) &&
    input.observation_ids.length >= 1 &&
    input.observation_ids.length <= 32 &&
    input.observation_ids.every(sourceId) &&
    new Set(input.observation_ids).size === input.observation_ids.length
  );
}
async function readPairedRecipe(file: File) {
  if (file.size > 65536) throw Error(t('The JSON source list exceeds 64 KiB.'));
  let value: unknown;
  try {
    value = JSON.parse(await file.text());
  } catch {
    throw Error(t('The file does not contain valid JSON.'));
  }
  if (!(
    fields(value, ['initial', 'neighbors', 'controls', 'repeats']) &&
    pair(value.initial) &&
    fields(value.controls, ['lower', 'upper']) &&
    pair(value.controls.lower) &&
    pair(value.controls.upper) &&
    Array.isArray(value.neighbors) &&
    value.neighbors.length >= 10 &&
    value.neighbors.length <= 22 &&
    value.neighbors.every(pair) &&
    Array.isArray(value.repeats) &&
    value.repeats.length >= 1 &&
    value.repeats.length <= 2 &&
    value.repeats.every(pair)
  ))
    throw Error(
      t(
        'Expected initial, neighbors, controls and repeats with original sweep and observation IDs.',
      ),
    );
  return value;
}

export function supportsPairedRechecks(
  desk: Desk,
  sessionId = desk.project?.active_session_id,
): boolean {
  const project = desk.project;
  if (
    desk.capabilities?.features.paired_event_rechecks !== true ||
    !project?.analyzer_stream_id ||
    !project.receiver_stream_id
  )
    return false;
  const streams = desk.sessions.find((session) => session.id === sessionId)?.streams;
  const analyzer =
    streams?.find((stream) => stream.role === 'spectrum_analyzer') ?? project.profile?.analyzer;
  const receiver =
    streams?.find((stream) => stream.role === 'receiver') ?? project.profile?.receiver;
  const a = analyzer?.settings.adapter,
    r = receiver?.settings.adapter;
  return (
    (a === 'tinysa-serial' && r === 'ardv1-serial') || (a === 'tinysa-sim' && r === 'ardv1-sim')
  );
}

export function PairedRechecks({
  desk,
  eventId,
  editable = false,
}: {
  desk: Desk;
  eventId: string;
  editable?: boolean;
}) {
  const [report, setReport] = useState<PairedRecheckResult>();
  const [file, setFile] = useState<File>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const readScope = useRef(new ResourceScope());
  const route = 'events/' + encodePathSegment(eventId) + '/paired-rechecks';
  useEffect(() => {
    const owned = readScope.current;
    const handle = owned.next();
    setLoading(true);
    setReport(undefined);
    setError('');
    void desk.api
      .project<PairedRecheckResult>(desk.projectId, route, undefined, handle.signal)
      .then((result) => {
        if (owned.current(handle.generation)) setReport(result);
      })
      .catch((cause: unknown) => {
        if (owned.current(handle.generation))
          setError(cause instanceof Error ? cause.message : t('Core request failed'));
      })
      .finally(() => {
        if (owned.current(handle.generation)) setLoading(false);
      });
    return () => owned.dispose();
  }, [desk.api, desk.projectId, route, revision]);

  const upload = async () => {
    if (!file || loading || desk.busy) return;
    const owner = readScope.current.next();
    setError('');
    try {
      const refs = await readPairedRecipe(file);
      if (!readScope.current.current(owner.generation)) return;
      const result = await desk.run(
        'Evaluate paired recheck',
        async () => {
          try {
            return await desk.api.project<PairedRecheckResult>(desk.projectId, route, refs);
          } catch (failure) {
            if (readScope.current.current(owner.generation))
              setError(failure instanceof Error ? failure.message : t('Core request failed'));
            throw failure;
          }
        },
        [],
      );
      if (readScope.current.current(owner.generation) && result) {
        setReport(result);
      }
    } catch (cause) {
      if (readScope.current.current(owner.generation))
        setError(cause instanceof Error ? cause.message : t('Core request failed'));
    }
  };
  return (
    <section aria-label={t('Paired recheck')} className="paired-rechecks">
      <h3>{t('Paired recheck')}</h3>
      {loading && <p role="status">{t('Reading evidence from the archive…')}</p>}
      {error && (
        <p className="error-text" role="alert">
          {t(error)}
        </p>
      )}
      {report?.state === 'missing_evidence' && (
        <p>{t('No paired visits have been recorded for this event.')}</p>
      )}
      {report?.state === 'evaluated' && (
        <>
          <p>
            <strong>
              {hz(report.frequency_hz)} · {report.instrument}
            </strong>
            {report.simulated && <> · {t('Training data')}</>}
          </p>
          <dl className="status-list">
            <div>
              <dt>{t('Signal repeated')}</dt>
              <dd>
                {report.signal_repeats} / {report.repeat_total}
              </dd>
            </div>
            <div>
              <dt>{t('Discrepancy repeated')}</dt>
              <dd>
                {report.discrepancy_repeats} / {report.repeat_total}
              </dd>
            </div>
          </dl>
          <ol>
            {report.visits.map((visit, i) => (
              <li key={visit.sweep_id + '/' + i}>
                <strong>
                  {t('tinySA difference')} {formatQuantity(visit.tiny_delta_db, 2)} {t('dB')} ·{' '}
                  {t('AR-DV1 drop')} {formatQuantity(visit.ar_drop_counts, 2)} {t('s_meter_count')}
                </strong>
                <div>
                  {t('Signal repeated')} · {t(visit.signal_repeated ? 'Yes' : 'No')}
                </div>
                <div>
                  {t('Discrepancy repeated')} · {t(visit.discrepancy_repeated ? 'Yes' : 'No')}
                </div>
                <details>
                  <summary>{t('Original source IDs')}</summary>
                  <div className="mono break">{visit.sweep_id}</div>
                  {visit.observation_ids.map((id) => (
                    <div className="mono break" key={id}>
                      {id}
                    </div>
                  ))}
                </details>
              </li>
            ))}
          </ol>
        </>
      )}
      {editable && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void upload();
          }}
        >
          <label>
            {t('Archived source list (JSON, up to 64 KiB)')}
            <input
              type="file"
              accept=".json,application/json"
              disabled={desk.busy}
              onChange={(event) => {
                setFile(event.target.files?.[0]);
                setError('');
              }}
            />
          </label>
          <p className="muted">
            {t(
              'Supply IDs for the initial pair, neighboring frequencies, two controls and one or two repeat visits. The core reads levels from the archive.',
            )}
          </p>
          <button disabled={!file || loading || desk.busy}>{t('Evaluate paired recheck')}</button>
        </form>
      )}
      <button
        type="button"
        disabled={loading || desk.busy}
        onClick={() => setRevision((n) => n + 1)}
      >
        {t('Refresh paired evidence')}
      </button>
    </section>
  );
}
