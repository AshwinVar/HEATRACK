/// Build-time configuration via --dart-define. No secrets: the Supabase anon key is a
/// public client key; authorization is enforced by the backend.
class AppConfig {
  static const apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://10.0.2.2:8000',
  );

  /// "supabase" (real accounts) or "dev" (locally minted tokens; development backend only).
  static const authMode = String.fromEnvironment(
    'AUTH_MODE',
    defaultValue: 'dev',
  );
  static const supabaseUrl = String.fromEnvironment('SUPABASE_URL');
  static const supabaseAnonKey = String.fromEnvironment('SUPABASE_ANON_KEY');

  static bool get isDevAuth => authMode != 'supabase';
}
