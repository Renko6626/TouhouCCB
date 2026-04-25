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

    // ===== 工业风按钮 =====
    // 主按钮：黑底白字，4px 灰偏移阴影；hover 微抬升 + 阴影增大
    ['btn-primary', 'inline-block px-5 py-2 bg-black text-white border-2 border-black font-semibold cursor-pointer shadow-[4px_4px_0_#444] transition-all duration-100 hover:translate-x-[-1px] hover:translate-y-[-1px] hover:shadow-[5px_5px_0_#444] disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-[4px_4px_0_#444] disabled:translate-x-0 disabled:translate-y-0'],
    // 次按钮：白底黑字，2px 黑偏移阴影
    ['btn-secondary', 'inline-block px-5 py-2 bg-white text-black border-2 border-black font-semibold cursor-pointer no-underline shadow-[2px_2px_0_#000] transition-all duration-100 hover:translate-x-[-1px] hover:translate-y-[-1px] hover:shadow-[3px_3px_0_#000]'],
    // 小按钮：行内操作 (表格内)
    ['btn-sm', 'inline-block px-3 py-1 bg-white text-black border border-black text-[13px] cursor-pointer hover:bg-black hover:text-white mr-1'],

    // ===== 模态框 =====
    ['modal-bg', 'fixed inset-0 bg-black/40 flex items-center justify-center p-4 z-1000'],
    ['modal-panel', 'bg-white border-[3px] border-black p-6 w-full shadow-[6px_6px_0_#000] max-h-[calc(100vh-32px)] overflow-y-auto'],

    // ===== 表格 =====
    ['table-wrap', 'mt-4 overflow-x-auto border-2 border-black bg-white shadow-[4px_4px_0_#000]'],
    ['th-industrial', 'bg-black text-white text-xs font-bold uppercase tracking-wider px-3 py-2 text-left whitespace-nowrap border border-gray-300'],
    ['td-industrial', 'border border-gray-300 px-3 py-2 text-left whitespace-nowrap'],
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
