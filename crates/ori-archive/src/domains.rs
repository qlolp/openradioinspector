use crate::*;
use rusqlite::{Connection, OptionalExtension, params, params_from_iter, types::Value as SqlValue};
use serde_json::{Value, json};

type CatalogSpec = (
    &'static str,
    &'static str,
    &'static str,
    fn(&rusqlite::Row<'_>) -> Result<Value>,
);

#[derive(Clone, Copy)]
enum Catalog {
    Candidates,
    Investigations,
    Measurements,
}
fn catalog(
    db: &Connection,
    kind: Catalog,
    session: Option<String>,
    filter: Option<String>,
    limit: usize,
    after: Option<MeasurementCursor>,
    maximum: usize,
) -> Result<Page<Value>> {
    let (table, time, filter_column, decode): CatalogSpec = match kind {
        Catalog::Candidates => ("candidates", "started_at", "state", rows::candidate),
        Catalog::Investigations => ("investigations", "created_at", "state", rows::investigation),
        Catalog::Measurements => (
            "measurement_results",
            "created_at",
            "methodology",
            rows::measurement,
        ),
    };
    let mut clauses = Vec::new();
    let mut arguments = Vec::new();
    if let Some(session) = session {
        clauses.push("session_id=?".into());
        arguments.push(SqlValue::Text(session));
    }
    if let Some(filter) = filter {
        clauses.push(format!("{filter_column}=?"));
        arguments.push(SqlValue::Text(filter));
    }
    if let Some(after) = after {
        clauses.push(format!("(context_utc({time}),context_utf8(id))>(?,?)"));
        arguments.push(SqlValue::Text(validate::utc(&after.started_at)?));
        arguments.push(SqlValue::Blob(after.id.as_bytes().to_vec()));
    }
    arguments.push(SqlValue::Integer(limit as i64 + 1));
    let clause = if clauses.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", clauses.join(" AND "))
    };
    let mut statement = db.prepare(&format!(
        "SELECT * FROM {table} {clause} ORDER BY context_utc({time}),context_utf8(id) LIMIT ?"
    ))?;
    let mut values = crate::queries::collect(
        statement.query(params_from_iter(arguments.iter()))?,
        decode,
        maximum,
    )?;
    let more = values.len() > limit;
    values.truncate(limit);
    let next_cursor = if more {
        values.last().map(|value| MeasurementCursor {
            started_at: if matches!(kind, Catalog::Candidates) {
                value["interval"][time].as_str().unwrap()
            } else {
                value[time].as_str().unwrap()
            }
            .into(),
            id: value["id"].as_str().unwrap().into(),
        })
    } else {
        None
    };
    Ok(Page {
        items: values,
        next_cursor,
    })
}
impl Archive {
    pub async fn stop_session(&self, value: Value, at: &str) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::StopSession {
            value,
            at: at.into(),
        }])
        .await
    }
    pub async fn get_candidate(&self, id: &str) -> Result<Option<Value>> {
        let id = id.to_owned();
        self.inner
            .read(move |db| {
                let mut statement = db.prepare("SELECT * FROM candidates WHERE id=?")?;
                let mut rows = statement.query([id])?;
                rows.next()?.map(rows::candidate).transpose()
            })
            .await
    }
    pub async fn get_investigation(&self, id: &str) -> Result<Option<(String, Value)>> {
        let id = id.to_owned();
        self.inner
            .read(move |db| {
                let mut statement = db.prepare("SELECT * FROM investigations WHERE id=?")?;
                let mut rows = statement.query([id])?;
                rows.next()?
                    .map(|row| Ok((row.get("session_id")?, rows::investigation(row)?)))
                    .transpose()
            })
            .await
    }
    pub async fn list_candidates(
        &self,
        session: &str,
        state: Option<&str>,
        limit: usize,
        after: Option<MeasurementCursor>,
    ) -> Result<Page<Value>> {
        validate_limit(limit, 1000)?;
        let session = session.to_owned();
        let filter = state.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                catalog(
                    db,
                    Catalog::Candidates,
                    Some(session),
                    filter,
                    limit,
                    after,
                    maximum,
                )
            })
            .await
    }
    pub async fn list_investigations(
        &self,
        session: Option<&str>,
        state: Option<&str>,
        limit: usize,
        after: Option<MeasurementCursor>,
    ) -> Result<Page<Value>> {
        validate_limit(limit, 1000)?;
        let session = session.map(str::to_owned);
        let filter = state.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                catalog(
                    db,
                    Catalog::Investigations,
                    session,
                    filter,
                    limit,
                    after,
                    maximum,
                )
            })
            .await
    }
    pub async fn list_open_investigations(
        &self,
        limit: usize,
        after: Option<MeasurementCursor>,
    ) -> Result<Vec<(String, Value)>> {
        validate_limit(limit, 1000)?;
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let mut arguments=Vec::new();let clause=if let Some(after)=after{arguments.push(SqlValue::Text(validate::utc(&after.started_at)?));arguments.push(SqlValue::Blob(after.id.as_bytes().to_vec()));"AND (context_utc(created_at),context_utf8(id))>(?,?)"}else{""};arguments.push(SqlValue::Integer(limit as i64));
            let mut statement=db.prepare(&format!("SELECT * FROM investigations WHERE state IN ('queued','tuning','listening','decoding') {clause} ORDER BY context_utc(created_at),context_utf8(id) LIMIT ?"))?;let mut rows=statement.query(params_from_iter(arguments.iter()))?;let mut remaining=maximum;let mut output=Vec::new();while let Some(row)=rows.next()?{let value=rows::investigation(row)?;let bytes=serde_json::to_vec(&value)?.len();if bytes>remaining{return Err(ArchiveError::QueryBudget);}remaining-=bytes;output.push((row.get("session_id")?,value));}Ok(output)
        }).await
    }
    pub fn runtime_reset_mutations(&self) -> Vec<Mutation> {
        vec![Mutation::ResetRuntimeStreamStates]
    }
    pub async fn reset_runtime_stream_states(&self) -> Result<CommittedBatch> {
        self.apply(self.runtime_reset_mutations()).await
    }
    pub async fn save_reference(&self, value: Value, event: EventDraft) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::Reference { value, event }]).await
    }
    pub async fn list_references(
        &self,
        session: &str,
        stream: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>> {
        validate_limit(limit, 100)?;
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
        let mut arguments=vec![SqlValue::Text(session)];let clause=if let Some(stream)=stream{arguments.push(SqlValue::Text(stream));"AND stream_id=?"}else{""};arguments.push(SqlValue::Integer(limit as i64));let mut statement=db.prepare(&format!("SELECT * FROM sweep_references WHERE session_id=? {clause} ORDER BY context_utc(created_at),context_utf8(id) LIMIT ?"))?;crate::queries::collect(statement.query(params_from_iter(arguments.iter()))?,rows::reference,maximum)
    }).await
    }
    pub async fn save_measurement_result(
        &self,
        value: Value,
        event: Option<EventDraft>,
    ) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::MeasurementResult { value, event }])
            .await
    }
    pub async fn list_measurement_results(
        &self,
        session: &str,
        method: Option<&str>,
        limit: usize,
        after: Option<MeasurementCursor>,
    ) -> Result<Page<Value>> {
        validate_limit(limit, 1000)?;
        let session = session.to_owned();
        let filter = method.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                catalog(
                    db,
                    Catalog::Measurements,
                    Some(session),
                    filter,
                    limit,
                    after,
                    maximum,
                )
            })
            .await
    }
    pub async fn put_artifact(&self, value: Artifact) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::Artifact(value)]).await
    }
    pub async fn get_artifact(
        &self,
        session: &str,
        stream: &str,
        id: &str,
    ) -> Result<Option<Artifact>> {
        let session = session.to_owned();
        let stream = stream.to_owned();
        let id = id.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        let timeout = self.inner.config.read_timeout;
        self.inner.read_with(timeout,true,move|db|{let size=db.query_row("SELECT length(payload) FROM instrument_artifacts WHERE session_id=? AND stream_id=? AND id=?",params![session,stream,id],|row|row.get::<_,i64>(0)).optional()?;if size.is_some_and(|size|size<0||size as u64>maximum as u64){return Err(ArchiveError::QueryBudget);}let mut statement=db.prepare("SELECT * FROM instrument_artifacts WHERE session_id=? AND stream_id=? AND id=?")?;let mut rows=statement.query(params![session,stream,id])?;let item=rows.next()?.map(rows::artifact).transpose()?;if item.as_ref().is_some_and(|item|item.payload.len()>maximum){return Err(ArchiveError::QueryBudget);}Ok(item)}).await
    }
    pub async fn list_artifacts(
        &self,
        session: &str,
        stream: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<Value>> {
        validate_limit(limit, 1000)?;
        if offset > 1_000_000 {
            return Err(error::invalid("artifact offset"));
        }
        let session = session.to_owned();
        let stream = stream.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{let mut statement=db.prepare("SELECT id,session_id,stream_id,schema_version,kind,mime_type,filename,sha256,created_at,length(payload) AS size_bytes,metadata_json FROM instrument_artifacts WHERE session_id=? AND stream_id=? ORDER BY context_utc(created_at) DESC,context_utf8(id) DESC LIMIT ? OFFSET ?")?;let mut rows=statement.query(params![session,stream,limit as i64,offset as i64])?;let mut remaining=maximum;let mut output=Vec::new();while let Some(row)=rows.next()?{crate::queries::push(&mut output,json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,"stream_id":row.get::<_,String>("stream_id")?,"schema_version":row.get::<_,i64>("schema_version")?,"kind":row.get::<_,String>("kind")?,"mime_type":row.get::<_,String>("mime_type")?,"filename":row.get::<_,String>("filename")?,"sha256":row.get::<_,String>("sha256")?,"created_at":validate::utc(&row.get::<_,String>("created_at")?)?,"size_bytes":row.get::<_,i64>("size_bytes")?,"metadata":rows::object(row.get("metadata_json")?)?}),&mut remaining)?;}Ok(output)}).await
    }
    pub async fn webhook_status(&self, project: &str) -> Result<Value> {
        let project = project.to_owned();
        self.inner
            .read(move |db| {
                let url = db
                    .query_row(
                        "SELECT url FROM monitoring_webhooks WHERE project_id=?",
                        [project],
                        |row| row.get::<_, String>(0),
                    )
                    .optional()?;
                Ok(json!({"configured":url.is_some(),"url":url}))
            })
            .await
    }
    pub async fn checkpoint(&self) -> Result<Value> {
        self.inner.checkpoint().await
    }
    pub async fn latest_events(&self, limit: usize) -> Result<Vec<Value>> {
        validate_limit(limit, 1000)?;
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{let mut statement=db.prepare("SELECT * FROM (SELECT * FROM events ORDER BY sequence DESC LIMIT ?) ORDER BY sequence")?;crate::queries::collect(statement.query([limit as i64])?,rows::event,maximum)}).await
    }
}
