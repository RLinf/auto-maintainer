// The guide's text markup, shared by the guide (pages/Guide.tsx) and the walkthroughs
// (tour.tsx): [[button]], {{key}}, [link](/path), **emphasis**, `code`.

import { Link } from "react-router-dom";

const TOKEN = /(\[\[[^\]]+\]\]|\{\{[^}]+\}\}|\[[^\]]+\]\([^)\s]+\)|\*\*[^*]+\*\*|`[^`]+`)/g;

/** The guide's markup: [[button]], {{key}}, [link](/path), **emphasis**, `code`. */
export function Rich({ text }: { text: string }) {
  const parts = text.split(TOKEN);
  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 0) return part;
        if (part.startsWith("[[")) return <span key={i} className="ui-chip">{part.slice(2, -2)}</span>;
        if (part.startsWith("{{")) return <span key={i} className="kbd">{part.slice(2, -2)}</span>;
        if (part.startsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
        if (part.startsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
        const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
        return link ? (
          <Link key={i} to={link[2]}>
            {link[1]}
          </Link>
        ) : (
          part
        );
      })}
    </>
  );
}
