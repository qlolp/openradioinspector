//! Immutable, bounded RF Explorer Normal reference selection. The survey group
//! signature includes hardware readback and grid and excludes only pass identity.
use super::*;
use crate::rows;

impl Archive {
    pub async fn duty_reference_block_count(&self, project: &str, baseline: &str) -> Result<usize> {
        let project = project.to_owned();
        let lo = format!("{baseline}.");
        let hi = format!("{baseline}/");
        self.inner.read(move |db| Ok(db.query_row(
            "SELECT COUNT(*) FROM monitoring_records WHERE kind='duty-reference-block' AND id>=? AND id<? AND project_id=?",
            params![lo,hi,project],|r|r.get::<_,u32>(0))? as usize)).await
    }

    pub async fn rfe_guard_source_context(&self, sweep: &str) -> Result<Option<String>> {
        let id = sweep.to_owned();
        self.inner.read(move |db| Ok(db.query_row(
            "SELECT lower(hex(p.signature)) FROM ori_rfe_tiles_v2 t JOIN ori_rfe_passes_v2 p ON p.pass_key=t.pass_key
             WHERE t.sweep_id=? AND p.signature IS NOT NULL", [id],|r|r.get(0)).optional()?)).await
    }

    pub async fn rfe_guard_sources_valid(
        &self,
        session: &str,
        stream: &str,
        keys: &[i64],
        signature: &str,
        windows: usize,
    ) -> Result<bool> {
        if !(20..=21).contains(&keys.len()) || !(1..=2398).contains(&windows) {
            return Ok(false);
        }
        let scope = Scope::new(session, stream);
        let keys = keys.to_vec();
        let signature = signature.to_ascii_uppercase();
        self.inner.read(move |db|{
            let mut stmt=db.prepare("SELECT 1 FROM ori_rfe_passes_v2 WHERE pass_key=?1 AND session_id=?2 AND stream_id=?3 AND hex(signature)=?4 AND received=?5")?;
            for key in keys {
                if !stmt.exists(params![key,scope.session,scope.stream,signature,windows as i64])? {return Ok(false);}
            }
            Ok(true)
        }).await
    }

    pub async fn rfe_guard_selection(&self, session: &str, stream: &str) -> Result<Value> {
        let scope = Scope::new(session, stream);
        self.inner.read(move |db| {
            let empty=json!({"ready":false,"passes":0,"windows":0,"ready_windows":0,"elapsed_seconds":0,"pass_keys":[],"minimum_passes":20,"minimum_seconds":60});
            let select=|order:&str|->Result<Vec<Pass>>{
                let mut stmt=db.prepare(&format!("{SELECT} WHERE session_id=? AND stream_id=? ORDER BY started_at {order},pass_key {order} LIMIT 64"))?;
                let mut rows=stmt.query(params![&scope.session,&scope.stream])?;
                let mut out=Vec::new();
                while let Some(row)=rows.next()?{out.push(Pass::row(row)?);}
                Ok(out)
            };
            let recent=select("DESC")?;
            let Some(latest)=recent.first() else {return Ok(empty)};
            let Some(plan)=latest.plan.as_ref().filter(|p|p.detector=="normal" && !p.simulated && latest.signature.is_some()) else{return Ok(empty)};
            let compatible=|p:&Pass| p.signature==latest.signature && p.received==plan.tiles() && p.unique==plan.count();
            let mut sources=Vec::with_capacity(21);
            let mut newer:Option<&Pass>=None;
            for pass in recent.iter().filter(|p| compatible(p)) {
                if newer.is_some_and(|n|pass.end>n.start || pass.start==n.start) {continue;}
                sources.push(pass);
                newer=Some(pass);
                if sources.len()==20 {break;}
            }
            sources.reverse();
            let earliest=select("ASC")?;
            if sources.len()==20 && let Some(first)=earliest.iter().find(|p|compatible(p))
                && first.key!=sources[0].key && first.end<=sources[0].start {
                sources.insert(0,first);
            }
            let (mut min_seconds,mut ready_windows)=(0.,0);
            if !sources.is_empty() {
                let mut stmt=db.prepare("SELECT a.started_at,b.ended_at FROM ori_rfe_tiles_v2 a
                    JOIN ori_rfe_tiles_v2 b ON b.pass_key=?2 AND b.tile=a.tile WHERE a.pass_key=?1")?;
                let mut rows=stmt.query(params![sources[0].key,sources.last().unwrap().key])?;
                min_seconds=f64::MAX;
                while let Some(row)=rows.next()?{
                    let elapsed=seconds(&row.get::<_,String>(0)?,&row.get::<_,String>(1)?).unwrap_or(0.);
                    min_seconds=min_seconds.min(elapsed);
                    if sources.len()>=20 && elapsed>=60. {ready_windows+=1;}
                }
                if min_seconds==f64::MAX {min_seconds=0.;}
            }
            let keys=sources.iter().map(|p|p.key).collect::<Vec<_>>();
            let context=latest.signature.as_ref().unwrap().iter().map(|b|format!("{b:02x}")).collect::<String>();
            Ok(json!({"context_key":context,"ready":ready_windows==plan.tiles(),"windows":plan.tiles(),"ready_windows":ready_windows,
                "passes":keys.len(),"pass_keys":keys,"elapsed_seconds":min_seconds,"minimum_passes":20,"minimum_seconds":60,
                "start_hz":plan.start_hz,"stop_hz":plan.stop_hz}))
        }).await
    }

    pub async fn rfe_guard_window(
        &self,
        session: &str,
        stream: &str,
        pass_keys: &[i64],
        tile: usize,
        context: &str,
    ) -> Result<Vec<Value>> {
        if !(20..=21).contains(&pass_keys.len()) || !(1..=2398).contains(&tile) {
            return Err(invalid("RF Explorer reference scope"));
        }
        let keys = pass_keys.to_vec();
        let scope = Scope::new(session, stream);
        let signature = context.to_ascii_uppercase();
        self.inner.read(move |db| {
            let mut stmt=db.prepare("SELECT s.* FROM ori_rfe_passes_v2 p JOIN ori_rfe_tiles_v2 t ON t.pass_key=p.pass_key
                JOIN sweeps s ON s.id=t.sweep_id WHERE p.pass_key=? AND p.session_id=? AND p.stream_id=? AND hex(p.signature)=? AND t.tile=?")?;
            let mut sources=vec![];
            for key in keys {
                let mut rows=stmt.query(params![key,scope.session,scope.stream,signature,tile as i64])?;
                sources.push(rows::sweep(rows.next()?.ok_or(ArchiveError::Conflict)?)?);
            }
            Ok(sources)
        }).await
    }
}
