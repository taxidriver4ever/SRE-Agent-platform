export class ApiError extends Error {
  constructor(public code: string, message: string, public status = 500) { super(message); }
}
