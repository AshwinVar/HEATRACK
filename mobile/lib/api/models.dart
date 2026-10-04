DateTime? _dt(Object? v) =>
    v == null ? null : DateTime.parse(v as String).toUtc();

class HealthProfileInfo {
  HealthProfileInfo({
    required this.id,
    required this.displayName,
    required this.timezone,
    required this.dataMode,
    required this.role,
    required this.categories,
    this.contactPhone,
  });

  factory HealthProfileInfo.fromJson(Map<String, dynamic> j) =>
      HealthProfileInfo(
        id: j['id'] as String,
        displayName: j['display_name'] as String,
        timezone: j['timezone'] as String,
        dataMode: j['data_mode'] as String,
        role: j['role'] as String,
        categories: (j['categories'] as List? ?? const []).cast<String>(),
        contactPhone: j['contact_phone'] as String?,
      );

  final String id;
  final String displayName;
  final String timezone;
  final String dataMode; // live | simulation
  final String role; // owner | caregiver
  final List<String> categories;
  final String? contactPhone;

  bool get isSimulation => dataMode == 'simulation';
  bool get isOwner => role == 'owner';
}

class Grant {
  Grant({
    required this.id,
    required this.status,
    required this.categories,
    this.caregiverName,
    this.caregiverEmail,
    this.wearerName,
    required this.createdAt,
  });

  factory Grant.fromJson(Map<String, dynamic> j) => Grant(
    id: j['id'] as String,
    status: j['status'] as String,
    categories: (j['categories'] as List).cast<String>(),
    caregiverName: j['caregiver_display_name'] as String?,
    caregiverEmail: j['caregiver_email'] as String?,
    wearerName: j['wearer_display_name'] as String?,
    createdAt: _dt(j['created_at'])!,
  );

  final String id;
  final String status;
  final List<String> categories;
  final String? caregiverName;
  final String? caregiverEmail;
  final String? wearerName;
  final DateTime createdAt;
}

class LatestMeasurement {
  LatestMeasurement({
    required this.value,
    required this.unit,
    required this.kind,
    required this.measuredAt,
    this.endAt,
    required this.receivedAt,
    required this.dataOrigin,
    this.deviceModel,
    required this.isSimulated,
  });

  factory LatestMeasurement.fromJson(Map<String, dynamic> j) =>
      LatestMeasurement(
        value: (j['value'] as num).toDouble(),
        unit: j['unit'] as String,
        kind: j['kind'] as String,
        measuredAt: _dt(j['measured_at'])!,
        endAt: _dt(j['end_at']),
        receivedAt: _dt(j['received_at'])!,
        dataOrigin: j['data_origin'] as String,
        deviceModel: (j['device'] as Map?)?['model'] as String?,
        isSimulated: j['is_simulated'] as bool? ?? false,
      );

  final double value;
  final String unit;
  final String kind;
  final DateTime measuredAt;
  final DateTime? endAt;
  final DateTime receivedAt;
  final String dataOrigin;
  final String? deviceModel;
  final bool isSimulated;
}

/// unsupported | permission_denied | waiting_for_data | fresh | stale
class MetricStatus {
  MetricStatus({
    required this.metric,
    required this.state,
    this.reason,
    this.latest,
    required this.budgetSeconds,
    this.ageSeconds,
  });

  factory MetricStatus.fromJson(Map<String, dynamic> j) => MetricStatus(
    metric: j['metric'] as String,
    state: j['state'] as String,
    reason: j['reason'] as String?,
    latest: j['latest'] == null
        ? null
        : LatestMeasurement.fromJson(j['latest'] as Map<String, dynamic>),
    budgetSeconds: j['freshness_budget_seconds'] as int,
    ageSeconds: j['age_seconds'] as int?,
  );

  final String metric;
  final String state;
  final String? reason;
  final LatestMeasurement? latest;
  final int budgetSeconds;
  final int? ageSeconds;

  bool get isFresh => state == 'fresh';
}

class AlertItem {
  AlertItem({
    required this.id,
    required this.profileId,
    required this.kind,
    this.metric,
    required this.category,
    required this.status,
    required this.reason,
    required this.isSimulated,
    required this.openedAt,
    this.acknowledgedAt,
    this.resolvedAt,
    this.resolutionReason,
    this.evidence,
    this.notifications,
    this.wearerTimezone = 'UTC',
  });

  factory AlertItem.fromJson(Map<String, dynamic> j) => AlertItem(
    id: j['id'] as String,
    profileId: j['health_profile_id'] as String,
    kind: j['kind'] as String,
    metric: j['metric'] as String?,
    category: j['category'] as String,
    status: j['status'] as String,
    reason: j['reason'] as String,
    isSimulated: j['is_simulated'] as bool? ?? false,
    openedAt: _dt(j['opened_at'])!,
    acknowledgedAt: _dt(j['acknowledged_at']),
    resolvedAt: _dt(j['resolved_at']),
    resolutionReason: j['resolution_reason'] as String?,
    evidence: j['evidence'] as Map<String, dynamic>?,
    notifications: j['notifications'] as Map<String, dynamic>?,
    wearerTimezone: j['wearer_timezone'] as String? ?? 'UTC',
  );

  final String id;
  final String profileId;
  final String kind;
  final String? metric;
  final String category;
  final String status;
  final String reason;
  final bool isSimulated;
  final DateTime openedAt;
  final DateTime? acknowledgedAt;
  final DateTime? resolvedAt;
  final String? resolutionReason;
  final Map<String, dynamic>? evidence;
  final Map<String, dynamic>? notifications;
  final String wearerTimezone;
}

class MeasurementPoint {
  MeasurementPoint({
    required this.t,
    required this.value,
    required this.origin,
    required this.simulated,
  });

  factory MeasurementPoint.fromJson(Map<String, dynamic> j) => MeasurementPoint(
    t: _dt(j['t'])!,
    value: (j['value'] as num).toDouble(),
    origin: j['origin'] as String,
    simulated: j['simulated'] as bool? ?? false,
  );

  final DateTime t;
  final double value;
  final String origin;
  final bool simulated;
}

class Dashboard {
  Dashboard({
    required this.profile,
    required this.generatedAt,
    required this.dataMode,
    this.banner,
    required this.summaryText,
    required this.healthThresholdsConfigured,
    required this.metrics,
    required this.unavailable,
    required this.daily,
    this.collector,
    required this.openAlerts,
    required this.insights,
  });

  factory Dashboard.fromJson(Map<String, dynamic> j) => Dashboard(
    profile: HealthProfileInfo.fromJson(j['profile'] as Map<String, dynamic>),
    generatedAt: _dt(j['generated_at'])!,
    dataMode: j['data_mode'] as String,
    banner: j['banner'] as String?,
    summaryText: j['summary_text'] as String,
    healthThresholdsConfigured:
        j['health_thresholds_configured'] as bool? ?? false,
    metrics: (j['metrics'] as List)
        .map((e) => MetricStatus.fromJson(e as Map<String, dynamic>))
        .toList(),
    unavailable: (j['unavailable_metrics'] as Map).cast<String, String>(),
    daily: (j['daily'] as List).cast<Map<String, dynamic>>(),
    collector: j['collector'] as Map<String, dynamic>?,
    openAlerts: (j['open_alerts'] as List)
        .map((e) => AlertItem.fromJson(e as Map<String, dynamic>))
        .toList(),
    insights: (j['insights'] as List).cast<Map<String, dynamic>>(),
  );

  final HealthProfileInfo profile;
  final DateTime generatedAt;
  final String dataMode;
  final String? banner;
  final String summaryText;
  final bool healthThresholdsConfigured;
  final List<MetricStatus> metrics;
  final Map<String, String> unavailable;
  final List<Map<String, dynamic>> daily;
  final Map<String, dynamic>? collector;
  final List<AlertItem> openAlerts;
  final List<Map<String, dynamic>> insights;

  bool get isSimulation => dataMode == 'simulation';
  MetricStatus? metric(String m) =>
      metrics.where((x) => x.metric == m).firstOrNull;
}
