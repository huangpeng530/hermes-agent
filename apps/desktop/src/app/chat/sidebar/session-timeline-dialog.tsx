import { useEffect, useState } from 'react'

import { getSessionTimeline, type SessionTimelineResponse } from '@/api/sessions'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'

interface SessionTimelineDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  sessionId: string
  title: string
  profile?: string
}

function formatTimestamp(epoch: number | null | undefined): string {
  if (epoch == null || Number.isNaN(epoch)) {
    return '—'
  }

  return new Date(epoch * 1000).toLocaleString([], {
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    month: '2-digit',
    year: 'numeric'
  })
}

// Read-only cross-compression timeline for one session: the compression chain
// (root -> tip), each segment's window, where each segment was compacted, and
// every user prompt with its wall-clock timestamp. The ordering proof that
// survives context compaction of the *current* conversation — the same data
// the CLI's `hermes sessions timeline` prints.
export function SessionTimelineDialog({
  open,
  onOpenChange,
  sessionId,
  title,
  profile
}: SessionTimelineDialogProps) {
  const { t } = useI18n()
  const r = t.sidebar.row
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<SessionTimelineResponse | null>(null)

  useEffect(() => {
    if (!open || !sessionId) {
      return
    }

    let cancelled = false
    setLoading(true)
    setError(null)
    setData(null)
    void getSessionTimeline(sessionId, profile, { limit: 500 })
      .then(response => {
        if (!cancelled) {
          setData(response)
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [open, profile, sessionId])

  const segments = data?.segments

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-h-[80vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{r.timelineTitle(title)}</DialogTitle>
        </DialogHeader>

        {loading && (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Codicon name="loading" size="0.875rem" spinning />
            <span>{r.timelineLoading}</span>
          </div>
        )}

        {error && (
          <div className="flex items-center gap-2 py-2 text-sm text-destructive">
            <Codicon name="error" size="0.875rem" />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && !segments && data && (
          <div className="py-2 text-sm text-muted-foreground">{r.timelineNoSegments}</div>
        )}

        {segments && (
          <div className="space-y-4">
            {segments.map((segment, index) => (
              <div key={segment.session_id} className="space-y-1">
                {index > 0 && (
                  <div className="flex items-center gap-2 border-t pt-3 text-xs text-muted-foreground">
                    <Codicon name="collapse-all" size="0.875rem" />
                    <span>
                      {r.timelineBoundary(
                        formatTimestamp(
                          segment.last_in_segment_compaction ?? segments[index - 1]?.last_in_segment_compaction ?? null
                        )
                      )}
                    </span>
                  </div>
                )}
                <div className="text-sm font-medium">
                  {segment.title || segment.session_id}
                  {segment.source && <span className="ml-2 font-normal text-muted-foreground">{segment.source}</span>}
                  {segment.model && <span className="ml-2 font-normal text-muted-foreground">{segment.model}</span>}
                </div>
                <div className="text-xs text-muted-foreground">
                  {r.timelineWindow(formatTimestamp(segment.started_at), formatTimestamp(segment.ended_at))}
                </div>
                {(() => {
                  const inPlace = segment.in_session_compressions ?? []
                  if (inPlace.length === 0) {
                    return null
                  }
                  return (
                    <div className="space-y-0.5 rounded-md border border-border/60 bg-accent/40 p-2">
                      <div className="flex items-center gap-1.5 text-xs font-medium">
                        <Codicon name="history" size="0.875rem" />
                        <span>{r.timelineInSessionComps(inPlace.length)}</span>
                      </div>
                      <ul className="mt-1 space-y-0.5">
                        {inPlace.map(comp => (
                          <li key={comp.row_id} className="flex gap-2 text-xs">
                            <span className="shrink-0 tabular-nums text-muted-foreground">
                              {r.timelineInSessionComp(
                                formatTimestamp(comp.timestamp),
                                comp.goal || '—'
                              )}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )
                })()}
                <ul className="space-y-0.5">
                  {segment.prompts.map(prompt => (
                    <li key={prompt.row_id} className="flex gap-2 text-xs">
                      <span className="shrink-0 tabular-nums text-muted-foreground">
                        {formatTimestamp(prompt.timestamp)}
                      </span>
                      <span className="line-clamp-1">{prompt.preview || '—'}</span>
                    </li>
                  ))}
                  {segment.prompts.length === 0 && (
                    <li className="text-xs text-muted-foreground">{r.timelineEmpty}</li>
                  )}
                </ul>
              </div>
            ))}
          </div>
        )}

        <div className="flex justify-end">
          <Button onClick={() => onOpenChange(false)} type="button" variant="ghost">
            {t.common.close}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
