export function BrandMark({ className = "brand-mark" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" aria-hidden>
      <defs>
        <linearGradient id="tg-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#b9bdff" />
          <stop offset="1" stopColor="#5b62e8" />
        </linearGradient>
      </defs>
      <path d="M16 2 30 16 16 30 2 16Z" fill="url(#tg-g)" />
      <path d="M16 9 23 16 16 23 9 16Z" fill="#0e0f12" />
      <path d="M16 12.5 19.5 16 16 19.5 12.5 16Z" fill="#c7c9ff" opacity="0.9" />
    </svg>
  );
}
