// API接口类型定义 - 主入口文件
// 此文件重新导出所有类型，以保持向后兼容性

// 从各个模块重新导出类型
export * from './auth'
export * from './market'
export * from './user'
export * from './chart'
export * from './trade'
export * from './stream'
export * from './common'

// 注意：trade.ts中的Outcome类型依赖market.ts，需要确保导入顺序正确
