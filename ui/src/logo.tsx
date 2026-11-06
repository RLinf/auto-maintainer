// The auto-maintainer mark: an "A" drawn as a merge graph (two branches meeting at a
// merge commit) on a gradient tile, with a spark for the agents. The same drawing is in
// ui/public/favicon.svg (bolder, without the spark) and docs/images/logo.svg.

import { useId } from "react";

export function Logo({ size = 26, spark = true, title }: { size?: number; spark?: boolean; title?: string }) {
  const id = useId().replace(/:/g, "");
  const stroke = spark ? 2.5 : 3;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      style={{ flexShrink: 0, display: "block" }}
    >
      <defs>
        <linearGradient id={`${id}t`} x1="2" y1="1" x2="30" y2="31" gradientUnits="userSpaceOnUse">
          <stop stopColor="#4f5bf5" />
          <stop offset=".55" stopColor="#6d45f6" />
          <stop offset="1" stopColor="#9b3cf8" />
        </linearGradient>
        <linearGradient id={`${id}h`} x1="16" y1="0" x2="16" y2="18" gradientUnits="userSpaceOnUse">
          <stop stopColor="#fff" stopOpacity=".22" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill={`url(#${id}t)`} />
      <rect x=".5" y=".5" width="31" height="31" rx="8.5" fill={`url(#${id}h)`} stroke="#fff" strokeOpacity=".14" />
      <path d="M9.7 23.1 15.4 9.9M22.3 23.1 16.6 9.9M12.5 18h7" stroke="#fff" strokeWidth={stroke} strokeLinecap="round" />
      <circle cx="16" cy="8.7" r="3.05" fill={`url(#${id}t)`} stroke="#fff" strokeWidth={stroke * 0.92} />
      <circle cx="9.4" cy="23.6" r="2.55" fill="#fff" />
      <circle cx="22.6" cy="23.6" r="2.55" fill="#fff" />
      {spark && (
        <path
          d="M25.3 4.6c.25 1.65 1 2.4 2.65 2.65-1.65.25-2.4 1-2.65 2.65-.25-1.65-1-2.4-2.65-2.65 1.65-.25 2.4-1 2.65-2.65z"
          fill="#fff"
        />
      )}
    </svg>
  );
}
