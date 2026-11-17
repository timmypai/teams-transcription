"""Synthetic context data, not a Markdown parser or a runtime store substitute."""
def context_record(ident, text, *, revision=1, provenance='synthetic_source'):
    return {'source_id':ident,'logical_id':ident,'text':text,
        'locator':{'provenance':provenance,'source_id':ident,'revision':revision}}


def context_snapshot(meeting, records, *, version=1, finalized=False, completeness='partial'):
    return {'meeting_id':meeting,'records':records,'version':version,
        'finalized':finalized,'completeness':completeness}
