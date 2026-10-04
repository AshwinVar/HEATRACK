import 'package:intl/intl.dart';
import 'package:timezone/data/latest.dart' as tzdata;
import 'package:timezone/timezone.dart' as tz;

bool _initialized = false;

void initTimezones() {
  if (!_initialized) {
    tzdata.initializeTimeZones();
    _initialized = true;
  }
}

final _fmt = DateFormat('d MMM HH:mm');

/// Wearer-local time (her IANA zone, e.g. Asia/Kolkata -> "IST") followed by this device's
/// local time when the offsets differ, e.g. "4 Oct 14:05 IST (4 Oct 09:35 BST)".
String formatDual(
  DateTime utc,
  String wearerTz, {
  DateTime Function(DateTime)? toLocal,
}) {
  initTimezones();
  final w = tz.TZDateTime.from(utc, tz.getLocation(wearerTz));
  final wearer = '${_fmt.format(w)} ${w.timeZoneName}';
  final local = (toLocal ?? (d) => d.toLocal())(utc);
  if (local.timeZoneOffset == w.timeZoneOffset) return wearer;
  return '$wearer (${_fmt.format(local)} ${local.timeZoneName})';
}

String formatAge(int? seconds) {
  if (seconds == null) return 'never';
  if (seconds < 90) return '${seconds}s ago';
  final m = seconds ~/ 60;
  if (m < 90) return '$m min ago';
  final h = m ~/ 60;
  if (h < 48) return '$h h ago';
  return '${h ~/ 24} days ago';
}
