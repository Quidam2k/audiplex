package com.audiplex.app.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.floatPreferencesKey
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.audiplex.app.playback.MixCategory
import com.audiplex.app.playback.MixLevels
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "settings")

@Singleton
class SettingsStore @Inject constructor(
    @ApplicationContext private val context: Context
) : AuthTokenStore {
    private val serverUrlKey = stringPreferencesKey("server_url")
    private val downloadOnCellularKey = booleanPreferencesKey("download_on_cellular")
    private val authTokenKey = stringPreferencesKey("auth_token")
    private val usernameKey = stringPreferencesKey("username")
    private val sessionExpiredKey = booleanPreferencesKey("session_expired")
    private val djLinkEnabledKey = booleanPreferencesKey("dj_link_enabled")
    private val restoreManualQueuesKey = booleanPreferencesKey("restore_manual_queues")  // #3601
    // v2: the v1 key was advanced by a reporter that dropped every entry it
    // "reported" (no API client existed that early in startup), so the deaths
    // it was meant to capture were marked done without ever being sent. A new
    // key resets the watermark to zero and re-ships the history Android still
    // holds on the device (#2961).
    // v3 (#3021): bumping the key resets the watermark so the exit history
    // Android still holds — including the 2026-08-14 16:02:58Z death — re-ships
    // WITH its stack trace, which the v2 records were delivered without.
    // v4 (#3031): v3 re-shipped all 16 retained records and NOT ONE carried a
    // trace, including the CRASH. Nothing could say whether Android had no
    // trace for them or whether our read threw, because both produced the same
    // empty string. trace_status now distinguishes those, and this bump is
    // what makes the retained history re-ship once carrying it — the last
    // chance to put a verdict on the 08-14 crash before it rolls out of
    // Android's 16-record history.
    private val lastExitReportedAtKey = longPreferencesKey("last_exit_reported_at_v4")

    // #997/#3111: independent per-channel playback levels (0.0–1.0 linear,
    // applied as ExoPlayer controller.volume). Todd wanted MUSIC and AUDIOBOOK
    // dialable separately from the companion VOICE dial so raising one channel
    // doesn't drag the others with it. Default 1.0 = unchanged from before this
    // shipped. Music and Stream share the music dial; Audiobook has its own.
    // These are the BASE volume; Media3's auto-duck (agent speech) multiplies
    // ~0.2 on top of it, so the effective ducked level is dial × 0.2.
    private val musicVolumeKey = floatPreferencesKey("music_volume")
    private val audiobookVolumeKey = floatPreferencesKey("audiobook_volume")
    private val lastSleepBedIdKey = intPreferencesKey("last_sleep_bed_id")  // #3953
    private val sleepBedVolumeKey = floatPreferencesKey("sleep_bed_volume")  // #4018
    private val sleepBedHeadroomMigratedKey = booleanPreferencesKey("sleep_bed_headroom_migrated")  // #5131
    private val sleepBookLevelKey = floatPreferencesKey("sleep_book_level")  // #5282
    private val sleepMixAttenuateMigratedKey = booleanPreferencesKey("sleep_mix_attenuate_migrated")  // #5282
    private val sleepHoldIndexKey = intPreferencesKey("sleep_hold_index")  // #5282
    private val sleepHoldRouteKey = stringPreferencesKey("sleep_hold_route")  // #5282
    private val sleepHoldAtKey = longPreferencesKey("sleep_hold_at")  // #5282
    private val sleepHoldFinishedKey = booleanPreferencesKey("sleep_hold_finished")  // #5282
    private val djVoiceLevelKey = floatPreferencesKey("dj_voice_level")  // #5282 slice 2
    private val chimeLevelKey = floatPreferencesKey("chime_level")  // #5282 slice 2
    private val bedtimeShiftAppliedKey = floatPreferencesKey("bedtime_shift_applied_db")  // #5282 slice 2
    private val bedtimeShiftCappedKey = floatPreferencesKey("bedtime_shift_capped_db")  // #5282 slice 2
    private val skipSecondsKey = intPreferencesKey("skip_seconds")  // #4220

    override val serverUrl: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[serverUrlKey] ?: ""
    }

    val downloadOnCellular: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[downloadOnCellularKey] ?: false
    }

    /**
     * Whether the always-on DJ link service runs (#3022).
     *
     * Defaults ON per Todd's ruling, and stays a plain toggle on purpose: his
     * framing was "we can change our mind later if it ends up not being a good
     * idea", so turning this off must return the app to exactly its previous
     * behaviour rather than disabling DJ control. Nothing may become
     * load-bearing on the service being up.
     */
    val djLinkEnabled: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[djLinkEnabledKey] ?: true
    }

    /**
     * #3601: after the app is killed, a DJ-built queue comes back paused on its
     * own. Todd's own queues stay as before (gone) unless this is on.
     */
    val restoreManualQueues: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[restoreManualQueuesKey] ?: false
    }

    suspend fun setRestoreManualQueues(enabled: Boolean) {  // #3601
        context.dataStore.edit { prefs -> prefs[restoreManualQueuesKey] = enabled }
    }

    override val authToken: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[authTokenKey] ?: ""
    }

    val username: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[usernameKey] ?: ""
    }

    /**
     * True when the session ended because the server rejected our token,
     * rather than because the user chose to sign out. Lets the login screen
     * explain itself instead of appearing for no visible reason — the old
     * behavior, which is how a silently-expiring token looked like a bug.
     */
    val sessionExpired: Flow<Boolean> = context.dataStore.data.map { prefs ->
        prefs[sessionExpiredKey] ?: false
    }

    /**
     * Timestamp of the newest process-exit record already shipped to the
     * server, so a restart reports each death exactly once (#2961).
     */
    val lastExitReportedAt: Flow<Long> = context.dataStore.data.map { prefs ->
        prefs[lastExitReportedAtKey] ?: 0L
    }

    suspend fun setLastExitReportedAt(timestamp: Long) {
        context.dataStore.edit { prefs ->
            prefs[lastExitReportedAtKey] = timestamp
        }
    }

    // #997/#3111: per-channel playback levels. Default 1.0 (unchanged).
    val musicVolume: Flow<Float> = context.dataStore.data.map { prefs ->
        prefs[musicVolumeKey] ?: 1.0f
    }

    val audiobookVolume: Flow<Float> = context.dataStore.data.map { prefs ->
        prefs[audiobookVolumeKey] ?: 1.0f
    }

    suspend fun setMusicVolume(volume: Float) {
        context.dataStore.edit { prefs ->
            prefs[musicVolumeKey] = volume.coerceIn(0f, 1f)
        }
    }

    suspend fun setAudiobookVolume(volume: Float) {
        context.dataStore.edit { prefs ->
            prefs[audiobookVolumeKey] = volume.coerceIn(0f, 1f)
        }
    }

    /** #4220: the audiobook skip buttons' size, picked by long-press. */
    val skipSeconds: Flow<Int> = context.dataStore.data.map { prefs ->
        com.audiplex.app.playback.SkipInterval.coerce(prefs[skipSecondsKey])
    }

    suspend fun setSkipSeconds(seconds: Int) {  // #4220
        context.dataStore.edit { prefs -> prefs[skipSecondsKey] = com.audiplex.app.playback.SkipInterval.coerce(seconds) }
    }

    /** #3953: the sleep bed last started, so the sleep dialog preselects it next time. */
    val lastSleepBedId: Flow<Int?> = context.dataStore.data.map { prefs -> prefs[lastSleepBedIdKey] }

    suspend fun setLastSleepBedId(id: Int) {
        context.dataStore.edit { prefs -> prefs[lastSleepBedIdKey] = id }
    }

    /** #4018: the sleep bed's own level, independent of the book's dial. */
    val sleepBedVolume: Flow<Float> = context.dataStore.data.map { prefs ->
        prefs[sleepBedVolumeKey] ?: com.audiplex.app.playback.SleepBedRules.DEFAULT_VOLUME
    }

    suspend fun setSleepBedVolume(volume: Float) {
        context.dataStore.edit { prefs -> prefs[sleepBedVolumeKey] = volume.coerceIn(0f, MixLevels.MAX_LEVEL) }
    }

    /** #5282: "Book at bedtime", applied only around sleep mode; the day dial is untouched. */
    val sleepBookLevel: Flow<Float> = context.dataStore.data.map { prefs ->
        prefs[sleepBookLevelKey] ?: com.audiplex.app.playback.SleepBedRules.DEFAULT_BOOK_LEVEL
    }

    suspend fun setSleepBookLevel(level: Float) {
        context.dataStore.edit { prefs ->
            prefs[sleepBookLevelKey] = level.coerceIn(0f, 1f)
            prefs.remove(bedtimeShiftCappedKey)  // he has set it himself, so a note about the cap no longer holds
        }
    }

    /** #5282: once, move the saved bed boost onto the book, keeping the balance he last set. */
    suspend fun migrateSleepMixToAttenuate() {
        context.dataStore.edit { prefs ->
            com.audiplex.app.playback.SleepBedRules.migrateToAttenuate(
                prefs[sleepBedVolumeKey],
                prefs[sleepBedHeadroomMigratedKey] == true,
                prefs[sleepMixAttenuateMigratedKey] == true,
            )?.let {
                prefs[sleepBedVolumeKey] = it.bedLevel
                prefs[sleepBookLevelKey] = it.bookLevel
            }
            prefs[sleepMixAttenuateMigratedKey] = true
        }
    }

    /** #5282 slice 2: the one place the levels are read and written. PlaybackManager applies them live. */
    private fun levelKey(category: MixCategory) = when (category) {
        MixCategory.MUSIC -> musicVolumeKey
        MixCategory.BOOK -> audiobookVolumeKey
        MixCategory.DJ_VOICE -> djVoiceLevelKey
        MixCategory.CHIMES -> chimeLevelKey
        MixCategory.BED -> sleepBedVolumeKey
        MixCategory.BOOK_BEDTIME -> sleepBookLevelKey
    }

    private fun defaultLevel(category: MixCategory) = when (category) {
        MixCategory.BED -> com.audiplex.app.playback.SleepBedRules.DEFAULT_VOLUME
        MixCategory.BOOK_BEDTIME -> com.audiplex.app.playback.SleepBedRules.DEFAULT_BOOK_LEVEL
        else -> 1f
    }

    val mixLevels: Flow<Map<MixCategory, Float>> = context.dataStore.data.map { prefs ->
        MixCategory.entries.associateWith { prefs[levelKey(it)] ?: defaultLevel(it) }
    }

    suspend fun setMixLevel(category: MixCategory, level: Float) {
        if (category == MixCategory.BOOK_BEDTIME) return setSleepBookLevel(level)
        context.dataStore.edit { prefs -> prefs[levelKey(category)] = level.coerceIn(0f, MixLevels.MAX_LEVEL) }
    }

    val djVoiceLevel: Flow<Float> = context.dataStore.data.map { prefs -> prefs[djVoiceLevelKey] ?: 1f }

    val chimeLevel: Flow<Float> = context.dataStore.data.map { prefs -> prefs[chimeLevelKey] ?: 1f }

    /** Once: the DJ voice starts at the music dial it used to follow. */
    suspend fun seedDjVoiceLevel() {
        context.dataStore.edit { prefs ->
            MixLevels.seedDjVoice(prefs[djVoiceLevelKey], prefs[musicVolumeKey])?.let { prefs[djVoiceLevelKey] = it }
        }
    }

    /** Dbs of the bedtime shift that did not fit below full. Zero when none. */
    val bedtimeShiftCappedDb: Flow<Float> = context.dataStore.data.map { prefs -> prefs[bedtimeShiftCappedKey] ?: 0f }

    /** Keeps "Book at bedtime" in step with the server's book raises. Read and written in one edit, so it cannot double. */
    suspend fun syncBedtimeShift(raisesOff: Boolean): MixLevels.BedtimeShift? {
        var result: MixLevels.BedtimeShift? = null
        context.dataStore.edit { prefs ->
            val shift = MixLevels.bedtimeShift(
                prefs[sleepBookLevelKey] ?: com.audiplex.app.playback.SleepBedRules.DEFAULT_BOOK_LEVEL,
                prefs[bedtimeShiftAppliedKey],
                raisesOff,
            ) ?: return@edit
            result = shift
            prefs[sleepBookLevelKey] = shift.level
            prefs[bedtimeShiftCappedKey] = shift.cappedDb
            val applied = shift.appliedDb
            if (applied == null) prefs.remove(bedtimeShiftAppliedKey) else prefs[bedtimeShiftAppliedKey] = applied
        }
        return result
    }

    /** #5282: survives the process dying overnight, because the next morning's first sound is the point. */
    suspend fun sleepHold(): com.audiplex.app.playback.SleepBedRules.SleepHold? {
        val prefs = context.dataStore.data.first()
        val index = prefs[sleepHoldIndexKey] ?: return null
        val route = prefs[sleepHoldRouteKey] ?: return null
        return com.audiplex.app.playback.SleepBedRules.SleepHold(
            index, route, prefs[sleepHoldAtKey] ?: 0L, prefs[sleepHoldFinishedKey] == true,
        )
    }

    suspend fun setSleepHold(hold: com.audiplex.app.playback.SleepBedRules.SleepHold?) {
        context.dataStore.edit { prefs ->
            if (hold == null) {
                prefs.remove(sleepHoldIndexKey)
                prefs.remove(sleepHoldRouteKey)
                prefs.remove(sleepHoldAtKey)
                prefs.remove(sleepHoldFinishedKey)
            } else {
                prefs[sleepHoldIndexKey] = hold.index
                prefs[sleepHoldRouteKey] = hold.route
                prefs[sleepHoldAtKey] = hold.atMs
                prefs[sleepHoldFinishedKey] = hold.finished
            }
        }
    }

    suspend fun setServerUrl(url: String) {
        context.dataStore.edit { prefs ->
            prefs[serverUrlKey] = url
        }
    }

    suspend fun setDownloadOnCellular(enabled: Boolean) {
        context.dataStore.edit { prefs ->
            prefs[downloadOnCellularKey] = enabled
        }
    }

    suspend fun setDjLinkEnabled(enabled: Boolean) {
        context.dataStore.edit { prefs ->
            prefs[djLinkEnabledKey] = enabled
        }
    }

    override suspend fun setAuthToken(token: String) {
        context.dataStore.edit { prefs ->
            prefs[authTokenKey] = token
            // Any successfully obtained token settles the expiry notice.
            prefs.remove(sessionExpiredKey)
        }
    }

    suspend fun clearAuthToken() {
        context.dataStore.edit { prefs ->
            prefs.remove(authTokenKey)
            prefs.remove(sessionExpiredKey)
        }
    }

    /**
     * Drop the token because the server rejected it, flagging the reason so
     * the login screen can say "session expired" rather than just appearing.
     */
    override suspend fun expireSession() {
        context.dataStore.edit { prefs ->
            prefs.remove(authTokenKey)
            prefs[sessionExpiredKey] = true
        }
    }

    suspend fun clearSessionExpired() {
        context.dataStore.edit { prefs ->
            prefs.remove(sessionExpiredKey)
        }
    }

    suspend fun setUsername(name: String) {
        context.dataStore.edit { prefs ->
            prefs[usernameKey] = name
        }
    }

    suspend fun clearUsername() {
        context.dataStore.edit { prefs ->
            prefs.remove(usernameKey)
        }
    }
}
