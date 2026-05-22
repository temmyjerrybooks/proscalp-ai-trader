/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"]
      },
      colors: {
        base: "#0b1020",
        panel: "#111827",
        panel2: "#162033",
        line: "#263244",
        mint: "#2dd4bf",
        amber: "#f59e0b",
        danger: "#ef4444"
      }
    }
  },
  plugins: []
};
