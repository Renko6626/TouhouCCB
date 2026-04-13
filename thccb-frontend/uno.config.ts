import { defineConfig, presetUno, presetAttributify, presetIcons } from 'unocss'

export default defineConfig({
  presets: [
    presetUno(),
    presetAttributify(),
    presetIcons({
      scale: 1.2,
      warn: true,
    }),
  ],
  shortcuts: [
    ['btn', 'px-4 py-1 inline-block bg-white text-black border-2 border-black cursor-pointer hover:bg-black hover:text-white disabled:cursor-default disabled:bg-white disabled:text-gray-500 disabled:opacity-70'],
    ['icon-btn', 'text-[0.9em] inline-block cursor-pointer select-none opacity-75 transition duration-200 ease-in-out hover:opacity-100 hover:text-black'],
    ['flex-center', 'flex justify-center items-center'],
    ['flex-between', 'flex justify-between items-center'],
    ['flex-col-center', 'flex flex-col justify-center items-center'],
    ['card', 'p-6 border-2 border-black bg-white shadow-[6px_6px_0_0_#000]'],
    ['input', 'px-3 py-2 border-2 border-black bg-white focus:outline-none focus:ring-0 focus:border-black'],
    ['badge', 'px-2 py-1 text-xs font-semibold border-2 border-black bg-white text-black'],
  ],
  theme: {
    colors: {
      // 工业风高对比度黑白主题
      primary: {
        50: '#ffffff',
        100: '#f5f5f5',
        200: '#eeeeee',
        300: '#dddddd',
        400: '#cccccc',
        500: '#aaaaaa',
        600: '#888888',
        700: '#666666',
        800: '#444444',
        900: '#222222',
      },
      success: {
        50: '#ffffff',
        100: '#f8f8f8',
        200: '#f0f0f0',
        300: '#e8e8e8',
        400: '#e0e0e0',
        500: '#d0d0d0',
        600: '#c0c0c0',
        700: '#b0b0b0',
        800: '#a0a0a0',
        900: '#909090',
      },
      warning: {
        50: '#ffffff',
        100: '#f0f0f0',
        200: '#e0e0e0',
        300: '#d0d0d0',
        400: '#c0c0c0',
        500: '#b0b0b0',
        600: '#a0a0a0',
        700: '#909090',
        800: '#808080',
        900: '#707070',
      },
      danger: {
        50: '#ffffff',
        100: '#f5f5f5',
        200: '#eeeeee',
        300: '#dddddd',
        400: '#cccccc',
        500: '#bbbbbb',
        600: '#aaaaaa',
        700: '#999999',
        800: '#888888',
        900: '#000000',
      },
      // 新增工业风关键颜色
      industrial: {
        black: '#000000',
        gray: '#444444',
        lightGray: '#e0e0e0',
        white: '#ffffff',
      },
      // 纯黑白对比色系
      contrast: {
        high: '#000000',
        medium: '#333333',
        low: '#666666',
        background: '#ffffff',
      }
    },
    breakpoints: {
      xs: '320px',
      sm: '640px',
      md: '768px',
      lg: '1024px',
      xl: '1280px',
      '2xl': '1536px',
    },
  },
})
