import type { ReactNode } from "react"
import type { components } from "@/lib/schema"

type Report = components["schemas"]["ReportDetail"]["report"]
type Item = Report["items"][number]

// Parse only explicit ISO dates. Relative/ambiguous deadlines remain unplaced.
function calendarDate(item: Item) {
  const value = item.due?.text ?? ""
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const date = new Date(`${value}T12:00:00Z`)
  return Number.isFinite(+date) && date.toISOString().slice(0, 10) === value
    ? value
    : null
}

function dateLabel(
  date: Date,
  language: string,
  options: Intl.DateTimeFormatOptions
) {
  try {
    return date.toLocaleDateString(language, options)
  } catch {
    return date.toLocaleDateString("en", options)
  }
}

export function ReportBlocks({
  report,
  renderItem,
  renderMedia,
}: {
  report: Report
  renderItem: (item: Item) => ReactNode
  renderMedia: (item: Item) => ReactNode
}) {
  const byId = new Map(report.items.map((item) => [item.id, item]))
  const covered = new Set(
    report.blocks?.flatMap((block) => block.item_ids) ?? []
  )
  return (
    <>
      {report.blocks?.map((block) => {
        const items = block.item_ids.map((id) => byId.get(id)!).filter(Boolean)
        const months = [
          ...new Set(
            items
              .map(calendarDate)
              .filter((date): date is string => !!date)
              .map((date) => date.slice(0, 7))
          ),
        ].sort()
        return (
          <section
            className={`report-widget report-widget-${block.kind}`}
            key={block.id}
          >
            <h2>{block.title}</h2>
            {block.kind === "actions" ? (
              <div className="report-table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Action</th>
                      <th>Owner</th>
                      <th>Deadline</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr key={item.id}>
                        <td>
                          {item.text}
                          {item.basis !== "explicit" && (
                            <small> · {item.basis}</small>
                          )}
                        </td>
                        <td>{item.owner?.text ?? "Not specified"}</td>
                        <td>{item.due?.text ?? "Not specified"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : block.kind === "calendar" ? (
              <>
                {months.map((month) => {
                  const [year, number] = month.split("-").map(Number)
                  const first = new Date(Date.UTC(year, number - 1, 1))
                  const days = new Date(Date.UTC(year, number, 0)).getUTCDate()
                  return (
                    <div className="report-calendar" key={month}>
                      <h3>
                        {dateLabel(first, report.language, {
                          month: "long",
                          year: "numeric",
                          timeZone: "UTC",
                        })}
                      </h3>
                      <div className="report-calendar-grid">
                        {Array.from({ length: 7 }, (_, day) => (
                          <div
                            className="report-weekday"
                            key={`weekday-${day}`}
                          >
                            {dateLabel(
                              new Date(Date.UTC(2024, 0, 1 + day)),
                              report.language,
                              {
                                weekday: "short",
                                timeZone: "UTC",
                              }
                            )}
                          </div>
                        ))}
                        {Array.from(
                          { length: (first.getUTCDay() + 6) % 7 },
                          (_, i) => (
                            <div aria-hidden="true" key={`blank-${i}`} />
                          )
                        )}
                        {Array.from({ length: days }, (_, i) => {
                          const date = `${month}-${String(i + 1).padStart(2, "0")}`
                          return (
                            <div className="report-calendar-day" key={date}>
                              <time dateTime={date}>{i + 1}</time>
                              {items
                                .filter((item) => calendarDate(item) === date)
                                .map((item) => (
                                  <p key={item.id}>
                                    {item.text}
                                    {item.owner && (
                                      <small>{item.owner.text}</small>
                                    )}
                                  </p>
                                ))}
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )
                })}
                {items.some((item) => !calendarDate(item)) && (
                  <div className="report-unplaced">
                    <h3>Dates to confirm</h3>
                    <p>
                      Relative or unspecified deadlines are shown as stated.
                    </p>
                    {items
                      .filter((item) => !calendarDate(item))
                      .map((item) => (
                        <p key={item.id}>
                          <strong>
                            {item.due?.text ?? "No deadline specified"}
                          </strong>{" "}
                          · {item.text}
                          {item.owner && <> · {item.owner.text}</>}
                        </p>
                      ))}
                  </div>
                )}
              </>
            ) : block.kind === "key_points" ? (
              <ul className="report-key-points">
                {items.map((item) => (
                  <li key={item.id}>
                    {item.text}
                    {item.basis !== "explicit" && (
                      <small> · {item.basis}</small>
                    )}
                  </li>
                ))}
              </ul>
            ) : null}
            {["actions", "calendar", "key_points"].includes(block.kind) &&
              items.map(renderMedia)}
            {["actions", "calendar", "key_points"].includes(block.kind) ? (
              <details className="report-block-evidence">
                <summary>Supporting evidence</summary>
                {items.map(renderItem)}
              </details>
            ) : (
              items.map(renderItem)
            )}
          </section>
        )
      })}
      {report.items.filter((item) => !covered.has(item.id)).map(renderItem)}
    </>
  )
}
