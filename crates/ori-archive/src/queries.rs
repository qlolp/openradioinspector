use crate::*;
use rusqlite::{Connection, OptionalExtension, params, params_from_iter, types::Value as SqlValue};
use serde_json::{Value, json};
use std::collections::HashMap;

fn text(value: impl Into<String>) -> SqlValue {
    SqlValue::Text(value.into())
}
fn cursor(value: &Value, time_field: &str) -> MeasurementCursor {
    MeasurementCursor {
        started_at: value[time_field]
            .as_str()
            .expect("decoded timestamp")
            .into(),
        id: value["id"].as_str().expect("decoded source ID").into(),
    }
}
pub(crate) fn push(output: &mut Vec<Value>, value: Value, remaining: &mut usize) -> Result<()> {
    let bytes = serde_json::to_vec(&value)?.len();
    if bytes > *remaining {
        return Err(ArchiveError::QueryBudget);
    }
    *remaining -= bytes;
    output.push(value);
    Ok(())
}
pub(crate) fn collect(
    mut rows: rusqlite::Rows<'_>,
    decode: fn(&rusqlite::Row<'_>) -> Result<Value>,
    maximum: usize,
) -> Result<Vec<Value>> {
    let mut remaining = maximum;
    let mut values = Vec::new();
    while let Some(row) = rows.next()? {
        push(&mut values, decode(row)?, &mut remaining)?;
    }
    Ok(values)
}
fn query_sweeps(db: &Connection, query: &SweepQuery, maximum: usize) -> Result<Page<Value>> {
    if query.session_id.is_none() && query.project_id.is_none() {
        return Err(error::invalid(
            "a session or project history scope is required",
        ));
    }
    if query.min_hz.is_some_and(|hz| hz < 0)
        || query.max_hz.is_some_and(|hz| hz > 9_007_199_254_740_991)
        || matches!((query.min_hz,query.max_hz),(Some(a),Some(b)) if a>b)
    {
        return Err(error::invalid("history frequency bounds"));
    }
    let start = query
        .start
        .as_deref()
        .map(validate::utc)
        .transpose()?
        .unwrap_or_else(|| "0000".into());
    let end = query
        .end
        .as_deref()
        .map(validate::utc)
        .transpose()?
        .unwrap_or_else(|| "9999".into());
    if start > end {
        return Err(error::invalid("history time bounds"));
    }
    let mut clauses = vec!["i.started_at>=?", "i.started_at<=?"];
    let mut arguments = vec![text(start), text(end)];
    if let Some(sid) = &query.session_id {
        clauses.push("i.session_id=?");
        arguments.push(text(sid));
    }
    if let Some(stream) = &query.stream_id {
        clauses.push("s.stream_id=?");
        arguments.push(text(stream));
    }
    if let Some(project) = &query.project_id {
        clauses.push("EXISTS(SELECT 1 FROM monitoring_sessions p WHERE p.project_id=? AND p.session_id=i.session_id)");
        arguments.push(text(project));
    }
    if let Some(event) = &query.event_id {
        let project = query
            .project_id
            .as_deref()
            .ok_or_else(|| error::invalid("event history requires a project scope"))?;
        let owned = db
            .query_row(
                "SELECT 1 FROM monitoring_records WHERE kind='event' AND id=? AND project_id=?",
                params![event, project],
                |row| row.get::<_, i64>(0),
            )
            .optional()?
            .is_some();
        if !owned {
            return Err(ArchiveError::NotFound);
        }
        clauses.push("EXISTS(SELECT 1 FROM monitoring_records e WHERE e.kind='event-source' AND e.project_id=? AND json_extract(e.payload,'$.event_id')=? AND json_extract(e.payload,'$.sweep_id')=s.id)");
        arguments.push(text(project));
        arguments.push(text(event));
    }
    if let Some(minimum) = query.min_hz {
        clauses.push("i.max_hz>=?");
        arguments.push(SqlValue::Integer(minimum));
    }
    if let Some(maximum) = query.max_hz {
        clauses.push("i.min_hz<=?");
        arguments.push(SqlValue::Integer(maximum));
    }
    if let Some(after) = &query.after {
        let stamp = validate::utc(&after.started_at)?;
        clauses.push("i.started_at<=? AND (i.started_at,context_utf8(i.sweep_id))<(?,?)");
        arguments.push(text(&stamp));
        arguments.push(text(stamp));
        arguments.push(SqlValue::Blob(after.id.as_bytes().to_vec()));
    }
    arguments.push(SqlValue::Integer(query.limit as i64 + 1));
    let sql = format!(
        "SELECT s.* FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE {} ORDER BY i.started_at DESC,context_utf8(i.sweep_id) DESC LIMIT ?",
        clauses.join(" AND ")
    );
    let mut statement = db.prepare(&sql)?;
    let mut items = collect(
        statement.query(params_from_iter(arguments.iter()))?,
        rows::sweep,
        maximum,
    )?;
    let more = items.len() > query.limit;
    items.truncate(query.limit);
    let next_cursor = if more {
        items.last().map(|value| MeasurementCursor {
            started_at: value["interval"]["started_at"]
                .as_str()
                .expect("decoded timestamp")
                .into(),
            id: value["id"].as_str().expect("decoded ID").into(),
        })
    } else {
        None
    };
    Ok(Page { items, next_cursor })
}
fn observations(
    db: &Connection,
    sid: &str,
    stream: Option<&str>,
    limit: usize,
    maximum: usize,
) -> Result<Vec<Value>> {
    let mut arguments = vec![text(sid)];
    let stream_clause = if let Some(stream) = stream {
        arguments.push(text(stream));
        " AND o.stream_id=?"
    } else {
        ""
    };
    arguments.push(SqlValue::Integer(limit as i64));
    let mut statement=db.prepare(&format!("SELECT o.*,p.level_bits_le FROM observations o LEFT JOIN ori_observation_level_bits p ON p.observation_id=o.id WHERE o.session_id=?{stream_clause} ORDER BY context_utc(o.started_at) DESC,context_utf8(o.id) DESC LIMIT ?"))?;
    let mut values = collect(
        statement.query(params_from_iter(arguments.iter()))?,
        rows::observation,
        maximum,
    )?;
    values.reverse();
    Ok(values)
}
impl Archive {
    /// Whole-source summaries; the caller freezes filters and boundary in its opaque cursor.
    pub async fn sweep_summaries(&self, query: SweepQuery, boundary: u64) -> Result<Page<Value>> {
        validate_limit(query.limit, 100)?;
        if boundary > i64::MAX as u64 {
            return Err(error::invalid("summary ingestion boundary"));
        }
        let sid = query
            .session_id
            .clone()
            .ok_or_else(|| error::invalid("summary session scope"))?;
        if query.project_id.is_some() || query.event_id.is_some() {
            return Err(error::invalid("summary project/event filters unsupported"));
        }
        if query.min_hz.is_some_and(|hz| hz < 0)
            || query.max_hz.is_some_and(|hz| hz > 9_007_199_254_740_991)
            || matches!((query.min_hz,query.max_hz),(Some(a),Some(b)) if a>b)
        {
            return Err(error::invalid("summary frequency bounds"));
        }
        let start = query.start.as_deref().map(validate::utc).transpose()?;
        let end = query.end.as_deref().map(validate::utc).transpose()?;
        if matches!((&start,&end),(Some(a),Some(b)) if a>=b) {
            return Err(error::invalid("summary time bounds"));
        }
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let current=db.query_row("SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion WHERE session_id=?",[&sid],|row|row.get::<_,i64>(0))?;
            if boundary>current as u64{return Err(error::invalid("summary boundary exceeds archive state"));}
            let mut clauses=vec!["m.session_id=?","i.sequence<=?","s.id=m.sweep_id","i.sweep_id=m.sweep_id"];
            let mut arguments=vec![text(sid),SqlValue::Integer(boundary as i64)];
            if let Some(stream)=query.stream_id{clauses.push("s.stream_id=?");arguments.push(text(stream));}
            if let Some(start)=start{clauses.push("m.started_at>=?");arguments.push(text(start));}
            if let Some(end)=end{clauses.push("m.started_at<?");arguments.push(text(end));}
            if let Some(minimum)=query.min_hz{clauses.push("m.max_hz>=?");arguments.push(SqlValue::Integer(minimum));}
            if let Some(maximum)=query.max_hz{clauses.push("m.min_hz<=?");arguments.push(SqlValue::Integer(maximum));}
            if let Some(after)=query.after{let stamp=validate::utc(&after.started_at)?;clauses.push("(m.started_at<? OR (m.started_at=? AND context_utf8(m.sweep_id)<?))");arguments.push(text(&stamp));arguments.push(text(stamp));arguments.push(SqlValue::Blob(after.id.into_bytes()));}
            arguments.push(SqlValue::Integer(query.limit as i64+1));
            let sql=format!("SELECT m.sweep_id FROM monitoring_sweep_index m INDEXED BY monitoring_sweep_index_scope_time CROSS JOIN sweeps s CROSS JOIN sweep_ingestion i WHERE {} ORDER BY m.started_at DESC,context_utf8(m.sweep_id) DESC LIMIT ?",clauses.join(" AND "));
            let ids=db.prepare(&sql)?.query_map(params_from_iter(arguments.iter()),|row|row.get::<_,String>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;
            let more=ids.len()>query.limit;let mut items=Vec::new();let mut remaining=maximum;
            for id in ids.into_iter().take(query.limit){
                let mut source=rows::get_sweep(db,&id)?.ok_or(ArchiveError::NotFound)?;validate::sweep(&mut source)?;
                let levels=source["levels"].as_array().expect("validated levels");let frequencies=source["frequencies_hz"].as_array().expect("validated frequencies");
                let mut peak=0;for i in 1..levels.len(){if levels[i].as_f64().unwrap()>levels[peak].as_f64().unwrap(){peak=i;}}
                let summary=json!({"id":source["id"],"session_id":source["session_id"],"stream_id":source["stream_id"],"interval":source["interval"],"level_unit":source["level_unit"],"point_count":levels.len(),"min_frequency_hz":frequencies.first(),"max_frequency_hz":frequencies.last(),"peak_frequency_hz":frequencies[peak],"peak_level":levels[peak]});
                push(&mut items,summary,&mut remaining)?;
            }
            let next_cursor=if more{items.last().map(|value|MeasurementCursor{started_at:value["interval"]["started_at"].as_str().unwrap().into(),id:value["id"].as_str().unwrap().into()})}else{None};
            Ok(Page{items,next_cursor})
        }).await
    }
    pub async fn sweep_page(&self, query: SweepQuery) -> Result<Page<Value>> {
        validate_limit(query.limit, 1000)?;
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| query_sweeps(db, &query, maximum))
            .await
    }
    pub async fn list_sessions(
        &self,
        limit: usize,
        after: Option<MeasurementCursor>,
    ) -> Result<Page<Value>> {
        validate_limit(limit, 1000)?;
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let mut arguments=Vec::new();let clause=if let Some(after)=after{arguments.push(text(validate::utc(&after.started_at)?));arguments.push(SqlValue::Blob(after.id.as_bytes().to_vec()));"WHERE (context_utc(created_at),context_utf8(id))>(?,?)"}else{""};arguments.push(SqlValue::Integer(limit as i64+1));
            let mut statement=db.prepare(&format!("SELECT id FROM sessions {clause} ORDER BY context_utc(created_at),context_utf8(id) LIMIT ?"))?;
            let ids=statement.query_map(params_from_iter(arguments.iter()),|row|row.get::<_,String>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;let more=ids.len()>limit;let mut items=Vec::new();let mut remaining=maximum;
            for id in ids.into_iter().take(limit){push(&mut items,rows::session(db,&id)?.ok_or(ArchiveError::NotFound)?,&mut remaining)?;}
            let next_cursor=if more{items.last().map(|v|cursor(v,"created_at"))}else{None};Ok(Page{items,next_cursor})
        }).await
    }
    pub async fn list_sweeps(
        &self,
        session: &str,
        stream: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>> {
        let mut query = SweepQuery::session(session);
        query.stream_id = stream.map(str::to_owned);
        query.limit = limit;
        let mut values = self.sweep_page(query).await?.items;
        values.reverse();
        Ok(values)
    }
    pub async fn list_observations(
        &self,
        session: &str,
        stream: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>> {
        validate_limit(limit, 1000)?;
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| observations(db, &session, stream.as_deref(), limit, maximum))
            .await
    }
    pub async fn sweep_context(
        &self,
        session: &str,
        id: &str,
        before: usize,
        after: usize,
    ) -> Result<Option<SweepContext>> {
        if before > 16 || after > 16 || id.trim().is_empty() || id.chars().count() > 128 {
            return Err(error::invalid("context count or source ID"));
        }
        let session = session.to_owned();
        let id = id.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let Some(mut anchor)=rows::get_sweep(db,&id)?else{return Ok(None)};if anchor["session_id"]!=session{return Ok(None);}validate::sweep(&mut anchor)?;if anchor["level_unit"]!="dBm"{return Err(ArchiveError::Corrupt("context requires original dBm sweeps".into()));}
            let stamp=anchor["interval"]["started_at"].as_str().unwrap();let stream=anchor["stream_id"].as_str().unwrap();
            let neighbours=|count:usize,preceding:bool|->Result<Vec<Value>>{
                if count==0{return Ok(Vec::new());}let (comparison,direction)=if preceding{("<","DESC")}else{(">","ASC")};let prefix=if preceding{"<="}else{">="};
                let sql=format!("SELECT s.* FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=? AND s.stream_id=? AND i.started_at{prefix}? AND (i.started_at,context_utf8(i.sweep_id)){comparison}(?,?) ORDER BY i.started_at {direction},context_utf8(i.sweep_id) {direction} LIMIT ?");
                let mut statement=db.prepare(&sql)?;let mut values=collect(statement.query(params![session,stream,stamp,stamp,id.as_bytes(),count as i64])?,rows::sweep,maximum)?;
                for value in &mut values{validate::sweep(value)?;if value["level_unit"]!="dBm"{return Err(ArchiveError::Corrupt("context unit mismatch".into()));}}
                if preceding{values.reverse();}Ok(values)
            };
            let before=neighbours(before,true)?;let after=neighbours(after,false)?;Ok(Some(SweepContext{anchor,before,after}))
        }).await
    }
    pub async fn measurement_owners(
        &self,
        ids: Vec<String>,
    ) -> Result<HashMap<String, Vec<(String, String)>>> {
        if ids.len() > 1000 {
            return Err(error::invalid("measurement ownership batch"));
        }
        self.inner.read(move|db|{
            let mut output=ids.iter().map(|id|(id.clone(),Vec::new())).collect::<HashMap<_,_>>();
            for ids in ids.chunks(400){if ids.is_empty(){continue;}let placeholders=std::iter::repeat_n("?",ids.len()).collect::<Vec<_>>().join(",");for table in ["sweeps","observations"]{let mut statement=db.prepare(&format!("SELECT id,session_id,stream_id FROM {table} WHERE id IN ({placeholders})"))?;let mut rows=statement.query(params_from_iter(ids.iter()))?;while let Some(row)=rows.next()?{let id:String=row.get(0)?;let owner=(row.get(1)?,row.get(2)?);let list=output.get_mut(&id).expect("requested owner");if !list.contains(&owner){list.push(owner);}}}}Ok(output)
        }).await
    }
    pub async fn sweep_ingestion_boundary(&self, session: &str) -> Result<u64> {
        let session = session.to_owned();
        self.inner
            .read(move |db| {
                Ok(db.query_row(
                    "SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion WHERE session_id=?",
                    [session],
                    |row| row.get::<_, i64>(0),
                )? as u64)
            })
            .await
    }
    pub async fn sweeps_after_ingestion(
        &self,
        session: &str,
        stream: Option<&str>,
        after: u64,
        limit: usize,
    ) -> Result<Vec<(u64, Value)>> {
        validate_limit(limit, 1000)?;
        if after > i64::MAX as u64 {
            return Err(error::invalid("ingestion cursor"));
        }
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{let mut arguments=vec![text(session),SqlValue::Integer(after as i64)];let clause=if let Some(stream)=stream{arguments.push(text(stream));" AND i.stream_id=?"}else{""};arguments.push(SqlValue::Integer(limit as i64));let mut statement=db.prepare(&format!("SELECT i.sequence,s.* FROM sweep_ingestion i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=? AND i.sequence>?{clause} ORDER BY i.sequence LIMIT ?"))?;let mut rows=statement.query(params_from_iter(arguments.iter()))?;let mut remaining=maximum;let mut output=Vec::new();while let Some(row)=rows.next()?{let value=rows::sweep(row)?;let bytes=serde_json::to_vec(&value)?.len();if bytes>remaining{return Err(ArchiveError::QueryBudget);}remaining-=bytes;output.push((row.get::<_,i64>("sequence")? as u64,value));}Ok(output)}).await
    }
    pub async fn monitoring(&self, kind: &str, id: &str) -> Result<Option<Value>> {
        if !validate::KINDS.contains(&kind) {
            return Err(error::invalid("monitoring kind"));
        }
        let kind = kind.to_owned();
        let id = id.to_owned();
        self.inner
            .read(move |db| {
                db.query_row(
                    "SELECT payload FROM monitoring_records WHERE kind=? AND id=?",
                    params![kind, id],
                    |row| row.get::<_, String>(0),
                )
                .optional()?
                .map(rows::object)
                .transpose()
            })
            .await
    }
    pub async fn put_monitoring(&self, kind: &str, value: Value) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::Monitoring {
            kind: kind.into(),
            value,
        }])
        .await
    }
    pub async fn list_monitoring(&self, kind: &str, query: MonitoringQuery) -> Result<Vec<Value>> {
        if !validate::KINDS.contains(&kind) {
            return Err(error::invalid("monitoring kind"));
        }
        validate_limit(query.limit, 1000)?;
        let kind = kind.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let mut arguments = vec![text(kind.clone())];
                let mut clauses = vec!["kind=?"];
                if let Some(project) = query.project_id {
                    clauses.push("project_id=?");
                    arguments.push(text(project));
                }
                if let Some(state) = query.state {
                    clauses.push("json_extract(payload,'$.state')=?");
                    arguments.push(text(state));
                }
                if !query.after.is_empty() {
                    if query.recent {
                        let created = db
                            .query_row(
                                "SELECT created_at FROM monitoring_records WHERE kind=? AND id=?",
                                params![kind, query.after],
                                |row| row.get::<_, String>(0),
                            )
                            .optional()?
                            .ok_or_else(|| error::invalid("monitoring cursor"))?;
                        clauses.push("(created_at,context_utf8(id))<(?,?)");
                        arguments.push(text(created));
                        arguments.push(SqlValue::Blob(query.after.as_bytes().to_vec()));
                    } else {
                        clauses.push("context_utf8(id)>?");
                        arguments.push(SqlValue::Blob(query.after.as_bytes().to_vec()));
                    }
                }
                arguments.push(SqlValue::Integer(query.limit as i64));
                let order = if query.recent {
                    "created_at DESC,context_utf8(id) DESC"
                } else {
                    "context_utf8(id)"
                };
                let mut statement = db.prepare(&format!(
                    "SELECT payload FROM monitoring_records WHERE {} ORDER BY {order} LIMIT ?",
                    clauses.join(" AND ")
                ))?;
                let mut rows = statement.query(params_from_iter(arguments.iter()))?;
                let mut remaining = maximum;
                let mut output = Vec::new();
                while let Some(row) = rows.next()? {
                    push(&mut output, rows::object(row.get(0)?)?, &mut remaining)?;
                }
                Ok(output)
            })
            .await
    }
    pub async fn monitoring_member(&self, project: &str, session: &str) -> Result<bool> {
        let project = project.to_owned();
        let session = session.to_owned();
        self.inner
            .read(move |db| {
                Ok(db
                    .query_row(
                        "SELECT 1 FROM monitoring_sessions WHERE project_id=? AND session_id=?",
                        params![project, session],
                        |row| row.get::<_, i64>(0),
                    )
                    .optional()?
                    .is_some())
            })
            .await
    }
    pub async fn capacity(&self) -> Result<Value> {
        let maximum = self.inner.config.max_database_bytes;
        self.inner.read(move|db|{let size=schema::database_bytes(db)?;let state=match maximum{None=>"unlimited",Some(limit)if size>=limit=>"full",Some(limit)if (size as u128)*10>=(limit as u128)*9=>"warning",Some(_)=>"ok"};Ok(json!({"storage_mode":"sqlite","database_bytes":size,"max_database_bytes":maximum,"remaining_bytes":maximum.map(|limit|limit.saturating_sub(size)),"state":state,"warning_threshold":0.9,"measurement_writes_blocked":state=="full","automatic_deletion":false}))}).await
    }
    pub async fn registry_counts(&self) -> Result<Value> {
        self.inner.read(move|db|Ok(db.query_row("SELECT (SELECT COUNT(*) FROM sessions),(SELECT COUNT(*) FROM sessions WHERE state='running'),(SELECT COUNT(*) FROM candidates),(SELECT COUNT(*) FROM investigations WHERE state='queued'),(SELECT COUNT(*) FROM investigations WHERE state IN ('tuning','listening','decoding')),(SELECT COALESCE(MAX(sequence),0) FROM events)",[],|row|Ok(json!({"sessions":row.get::<_,i64>(0)?,"running_sessions":row.get::<_,i64>(1)?,"candidates":row.get::<_,i64>(2)?,"queued_investigations":row.get::<_,i64>(3)?,"active_investigations":row.get::<_,i64>(4)?,"last_event_id":row.get::<_,i64>(5)?})))?)).await
    }
    pub async fn analytics_summary(&self, session: &str, stream: Option<&str>) -> Result<Value> {
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
        let mut arguments=vec![text(session.clone())];let clause=if let Some(stream)=&stream{arguments.push(text(stream));" AND stream_id=?"}else{""};
        let totals=|table:&str|->Result<Value>{let sql=format!("SELECT COUNT(*),MIN(context_utc(started_at)),MAX(context_utc(started_at)) FROM {table} WHERE session_id=?{clause}");let (count,first,last)=db.query_row(&sql,params_from_iter(arguments.iter()),|row|Ok((row.get::<_,i64>(0)?,row.get::<_,Option<String>>(1)?,row.get::<_,Option<String>>(2)?)))?;let mut statement=db.prepare(&format!("SELECT DISTINCT level_unit FROM {table} WHERE session_id=?{clause} ORDER BY context_utf8(level_unit)"))?;let units=statement.query_map(params_from_iter(arguments.iter()),|row|row.get::<_,String>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;Ok(json!({"count":count,"first_started_at":first,"last_started_at":last,"level_units":units}))};
        let mut sweeps=totals("sweeps")?;let mut receiver=totals("observations")?;let mut query=SweepQuery::session(&session);query.stream_id=stream.clone();query.limit=1;let latest=query_sweeps(db,&query,maximum)?.items.into_iter().next();
        let peak=if let Some(value)=&latest{let values=value["levels"].as_array().ok_or_else(||ArchiveError::Corrupt("sweep levels".into()))?;let mut index=0;for i in 1..values.len(){if values[i].as_f64().ok_or_else(||ArchiveError::Corrupt("sweep level".into()))?>values[index].as_f64().ok_or_else(||ArchiveError::Corrupt("sweep level".into()))?{index=i;}}json!({"sweep_id":value["id"],"frequency_hz":value["frequencies_hz"][index],"level":values[index],"level_unit":value["level_unit"]})}else{Value::Null};
        sweeps["latest"]=latest.unwrap_or(Value::Null);sweeps["latest_peak"]=peak;receiver["latest"]=observations(db,&session,stream.as_deref(),1,maximum)?.into_iter().next().unwrap_or(Value::Null);
        let (known,open)=db.query_row(&format!("SELECT COALESCE(SUM(squelch_open IS NOT NULL),0),COALESCE(SUM(squelch_open=1),0) FROM observations WHERE session_id=?{clause}"),params_from_iter(arguments.iter()),|row|Ok((row.get::<_,i64>(0)?,row.get::<_,i64>(1)?)))?;
        receiver["squelch_known"]=json!(known);receiver["squelch_open"]=json!(open);receiver["squelch_open_ratio"]=if known==0{Value::Null}else{json!((open as f64)/(known as f64))};Ok(json!({"session_id":session,"stream_id":stream,"archive_wide":true,"sweeps":sweeps,"observations":receiver}))
    }).await
    }
}
