/**
 * 表单验证工具
 * 提供常用的表单验证功能
 */

// 邮箱验证
export function validateEmail(email: string): boolean {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
  return emailRegex.test(email)
}

// 手机号验证（中国手机号）
export function validatePhone(phone: string): boolean {
  const phoneRegex = /^1[3-9]\d{9}$/
  return phoneRegex.test(phone)
}

// 密码强度验证（至少8位，包含大小写字母和数字）
export function validatePassword(password: string): {
  isValid: boolean
  errors: string[]
} {
  const errors: string[] = []
  
  if (password.length < 8) {
    errors.push('密码长度至少8位')
  }
  
  if (!/[a-z]/.test(password)) {
    errors.push('密码必须包含小写字母')
  }
  
  if (!/[A-Z]/.test(password)) {
    errors.push('密码必须包含大写字母')
  }
  
  if (!/\d/.test(password)) {
    errors.push('密码必须包含数字')
  }
  
  return {
    isValid: errors.length === 0,
    errors
  }
}

// 数字范围验证
export function validateRange(value: number, min: number, max: number): {
  isValid: boolean
  message: string
} {
  if (value < min) {
    return {
      isValid: false,
      message: `值不能小于${min}`
    }
  }
  
  if (value > max) {
    return {
      isValid: false,
      message: `值不能大于${max}`
    }
  }
  
  return {
    isValid: true,
    message: ''
  }
}

// 必填字段验证
export function validateRequired(value: string | number | undefined | null): {
  isValid: boolean
  message: string
} {
  if (value === undefined || value === null || value === '') {
    return {
      isValid: false,
      message: '此字段为必填项'
    }
  }
  
  return {
    isValid: true,
    message: ''
  }
}

// 交易份额验证
export function validateShares(shares: number, maxShares: number): {
  isValid: boolean
  message: string
} {
  if (shares <= 0) {
    return {
      isValid: false,
      message: '交易份额必须大于0'
    }
  }
  
  if (shares > maxShares) {
    return {
      isValid: false,
      message: `交易份额不能超过最大可交易份额(${maxShares})`
    }
  }
  
  return {
    isValid: true,
    message: ''
  }
}