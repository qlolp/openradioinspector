import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseJSON} from '../generated/typescript/codec.ts';
import * as runtime from '../generated/typescript/runtime.ts';

const fixture = () => parseJSON(readFileSync(new URL('../fixtures/history-runtime-v1.json', import.meta.url), 'utf8')) as any;
const sdk = runtime as unknown as Record<string, (value: any) => any>;
const bits = (value: number): bigint => {
    const view = new DataView(new ArrayBuffer(8));
    view.setFloat64(0, value);
    return view.getBigUint64(0);
};

test('history DTOs preserve signed microseconds, nullable uint64 boundary and nested exact JSON', () => {
    const f = fixture();
    const request = sdk.decodeHistoryRequest!(f.request);
    assert.equal(request.filters.started_at_us, -(1n << 63n));
    assert.equal(request.filters.ended_at_us, (1n << 63n) - 1n);
    assert.equal(request.filters.max_frequency_hz, (1n << 63n) - 1n);
    assert.equal(bits(request.threshold.value), 0x8000000000000000n);
    const requestRound = sdk.decodeHistoryRequest!(parseJSON(sdk.encodeHistoryRequest!(request)));
    assert.equal(bits(requestRound.threshold.value), 0x8000000000000000n);
    assert.equal(requestRound.filters.session_id, 'history-é-e\u0301/雪');
    const job = sdk.decodeHistoryJob!(f.job);
    assert.equal(job.source_boundary, (1n << 64n) - 1n);
    const round = sdk.decodeHistoryJob!(parseJSON(sdk.encodeHistoryJob!(job)));
    assert.equal(round.source_boundary, (1n << 64n) - 1n);
    assert.equal(round.failure, null);
    assert.equal(round.enforcement.future['é'], 1);
    assert.equal(round.enforcement.future['e\u0301'], 2);
    assert.equal(round.enforcement.future['$serde_json::private::Number'], 'literal');
    assert.equal(round.enforcement.future['$ori::escaped::$serde_json::private::Number'], 'literal-prefix');
    assert.equal(round.enforcement.future.big, 1n << 64n);
    assert.equal(bits(round.enforcement.future.negative_zero), 0x8000000000000000n);
    assert.equal(bits(round.enforcement.future.adjacent), 0xbff0000000000001n);
    const failed = sdk.decodeHistoryJob!(parseJSON(sdk.encodeHistoryJob!(sdk.decodeHistoryJob!(f.failed_job))));
    assert.equal(failed.source_boundary, null);
    assert.equal(failed.enforcement, null);
    assert.equal(failed.failure.code, 'source_record_invalid');
    const result = sdk.decodeHistoryJobResult!(parseJSON(sdk.encodeHistoryJobResult!(sdk.decodeHistoryJobResult!(f.result))));
    assert.equal(result.metrics.io_bytes, (1n << 64n) - 1n);
    assert.equal(result.metrics['$serde_json::private::Number'], 'metrics-key');
    assert.equal(result.enforcement.future['$serde_json::private::Number'], 'result-enforcement-key');
    assert.equal(result.data['é'], 1);
    assert.equal(result.data['e\u0301'], 2);
    assert.equal(bits(result.metrics.negative_zero), 0x8000000000000000n);
});

test('history viewport and cursor keep the original exact IDs, UTC, unit, point and float bits', () => {
    const f = fixture();
    const query = sdk.decodeHistoryViewportQuery!(f.viewport_query);
    assert.equal(query.start_us, -(1n << 63n));
    assert.equal(query.end_us, (1n << 63n) - 1n);
    assert.equal(query.cursor, null);
    const page = sdk.decodeHistoryViewportPage!(parseJSON(sdk.encodeHistoryViewportPage!(sdk.decodeHistoryViewportPage!(f.viewport_page))));
    assert.equal(page.derived, true);
    assert.equal(page.boundary, (1n << 64n) - 1n);
    assert.equal(page.rows[0].started_at_us, -(1n << 63n));
    assert.equal(page.rows[0].measurement_id, 'sweep-e\u0301/雪');
    assert.equal(page.rows[0].level_unit, 's_meter_count');
    assert.equal(page.rows[0].settings_sha256, null);
    assert.equal(bits(page.rows[0].cells[0].level), 0x8000000000000000n);
    assert.equal(bits(page.rows[0].cells[1].level), 0xbff0000000000001n);
    assert.equal(page.rows[0].cells[1].frequency_hz, (1n << 63n) - 1n);
    assert.equal(page.rows[0].cells[1].point_index, 65535);
    assert.equal(page.next_cursor.boundary, (1n << 64n) - 1n);
    assert.equal(page.next_cursor.after.started_at, '1969-12-31T23:59:59.999999Z');
});

test('history signed and unsigned formats reject overflow and a missing required null', () => {
    assert.doesNotThrow(() => sdk.decodeHistoryJob!(fixture().job));
    assert.doesNotThrow(() => sdk.decodeHistoryRequest!(fixture().request));
    for (const boundary of [-1n, 1n << 64n]) {
        assert.throws(() => sdk.decodeHistoryJob!({...fixture().job, source_boundary: boundary}));
    }
    const missing = fixture().job;
    delete missing.source_boundary;
    assert.throws(() => sdk.decodeHistoryJob!(missing));
    for (const started_at_us of [-(1n << 63n) - 1n, 1n << 63n]) {
        const request = fixture().request;
        request.filters.started_at_us = started_at_us;
        assert.throws(() => sdk.decodeHistoryRequest!(request));
    }
    const request = fixture().request;
    request.filters.max_frequency_hz = 1n << 63n;
    assert.throws(() => sdk.decodeHistoryRequest!(request));
    request.filters.max_frequency_hz = 1;
    request.filters.min_frequency_hz = -1;
    assert.throws(() => sdk.decodeHistoryRequest!(request));
    for (const page_size of [0, 501]) {
        assert.throws(() => sdk.decodeHistoryRequest!({...fixture().request, page_size}));
    }
    for (const columns of [0, 1025]) {
        assert.throws(() => sdk.decodeHistoryViewportQuery!({...fixture().viewport_query, columns}));
    }
});

test('history operation selection requires the advertised independent route feature', () => {
    const ids = (features: string[]) => runtime.selectRuntimeOperations('rust-candidate', features).map(op => op.operation_id as string);
    assert.ok(!ids([]).includes('post_api_v1_history_jobs'));
    assert.ok(ids(['derived_history_jobs']).includes('post_api_v1_history_jobs'));
    assert.ok(!ids(['derived_history_jobs']).includes('get_api_v1_history_waterfall'));
    assert.ok(ids(['derived_history_viewport']).includes('get_api_v1_history_waterfall'));
    assert.ok(!runtime.selectRuntimeOperations('python-legacy').some(op => (op.operation_id as string) === 'post_api_v1_history_jobs'));
});
