"""A live heartbeat is not evidence that new committed measurements arrive."""
import math


class SourceProgress:
    def __init__(self):self.previous={}
    def observe(self,row,*,planned=()):
        errors=[];now=row['at_ns']
        for role in ('analyzer','receiver'):
            limit=row.get(role+'_max_silence_seconds');count=row.get(role+'_sources');source=row.get(role+'_source_id')
            if type(limit) not in (int,float) or not math.isfinite(limit) or not 0<limit<=3600 \
                    or type(count) is not int or count<0 or (count>0 and (not isinstance(source,str) or not source)):
                errors.append(role+'_freshness_profile_missing');continue
            previous=self.previous.get(role)
            last=now if previous is None else previous[2]
            if previous:
                if count<previous[0]:errors.append(role+'_source_counter_regressed')
                elif count>previous[0] and source!=previous[1]:last=now
                elif count!=previous[0] or source!=previous[1]:errors.append(role+'_source_progress_unbound')
            for start,end,kind in planned:
                if kind=='sleep' or kind=='usb_'+role:
                    if start<=now<=end:last=now
                    elif last<end<=now:last=end
            self.previous[role]=(count,source,last)
            if now-last>limit*1_000_000_000:errors.append(role+'_source_stalled')
        return errors
