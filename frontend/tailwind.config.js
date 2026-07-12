/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: '#0a0f1e',
        panel: '#111b2e',
        panel2: '#182742',
        bull: '#16c784',
        bear: '#ff4d5a',
        neutral: '#a2acbf',
        accent: '#f0b90b',
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(240,185,11,0.18), 0 14px 40px rgba(0,0,0,0.28)'
      }
    },
  },
  plugins: [],
}
