import { getLogLineColor } from "./logLineStyles";

const ISO_TIMESTAMP_PREFIX = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s+(.+)$/;

function formatTimestamp(value: string): string | null {
  const normalized = value.replace(/\.(\d{3})\d+(?=Z|[+-]\d{2}:\d{2}$)/, ".$1");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return null;

  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function LogLine({ line }: { line: string }) {
  const match = line.match(ISO_TIMESTAMP_PREFIX);
  const timestamp = match ? formatTimestamp(match[1]) : null;

  return (
    <div className={`leading-relaxed ${getLogLineColor(line)}`}>
      {timestamp && match ? (
        <>
          <time
            dateTime={match[1]}
            title={match[1]}
            className="mr-2 whitespace-nowrap text-gray-500 tabular-nums"
          >
            {timestamp}
          </time>
          <span>{match[2]}</span>
        </>
      ) : line}
    </div>
  );
}
