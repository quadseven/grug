/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
        grotesk: ["Space Grotesk", "system-ui", "sans-serif"],
        display: ["Instrument Serif", "Times New Roman", "serif"],
      },
      colors: {
        cave: {
          bg: "#faf6ee",
          bg2: "#f3ecdb",
          paper: "#fffbf2",
          ink: "#181613",
          ink2: "#3a342c",
          muted: "#7a7064",
          amber: "#fbbf24",
          "amber-deep": "#d97706",
          tomato: "#e0502a",
          moss: "#3f6b3a",
          sky: "#2e5d8a",
        },
      },
      boxShadow: {
        brutal: "6px 6px 0 0 #181613",
        "brutal-sm": "3px 3px 0 0 #181613",
        "brutal-lg": "10px 10px 0 0 #181613",
      },
      animation: {
        "tape-scroll": "tape-scroll 40s linear infinite",
        blink: "blink 1.6s ease-in-out infinite",
      },
      keyframes: {
        "tape-scroll": {
          from: { transform: "translateX(0)" },
          to: { transform: "translateX(-100%)" },
        },
        blink: {
          "0%,100%": { opacity: "1" },
          "50%": { opacity: "0.2" },
        },
      },
    },
  },
  plugins: [],
};
