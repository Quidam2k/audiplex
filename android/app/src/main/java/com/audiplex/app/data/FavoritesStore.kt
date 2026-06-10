package com.audiplex.app.data

import com.audiplex.app.data.api.FavoriteCreate
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Cache of favorited entity keys, grouped by entity_type.
 *
 * Source of truth lives on the server (`/api/music/favorites`); this
 * holder caches the keys in memory so screens can render star icons
 * synchronously without re-fetching. Mutations write through to the
 * server and the local cache in parallel — if the network call fails,
 * the optimistic update is rolled back.
 */
@Singleton
class FavoritesStore @Inject constructor(
    private val apiHolder: ApiServiceHolder
) {
    // entity_type -> set of entity_keys
    private val _byType = MutableStateFlow<Map<String, Set<String>>>(emptyMap())
    val byType: StateFlow<Map<String, Set<String>>> = _byType

    fun keysOf(type: String): Set<String> = _byType.value[type].orEmpty()

    fun isFavorited(type: String, key: String): Boolean =
        keysOf(type).contains(key)

    suspend fun refresh() {
        val api = apiHolder.api ?: return
        val all = try { api.getFavorites() } catch (_: Exception) { return }
        _byType.value = all.groupBy({ it.entityType }, { it.entityKey })
            .mapValues { it.value.toSet() }
    }

    suspend fun toggle(type: String, key: String): Boolean {
        val isCurrently = isFavorited(type, key)
        return if (isCurrently) remove(type, key).let { false } else add(type, key).let { true }
    }

    suspend fun add(type: String, key: String) {
        // Optimistic update first.
        _byType.update { current ->
            val existing = current[type].orEmpty()
            current + (type to (existing + key))
        }
        try {
            apiHolder.api?.addFavorite(FavoriteCreate(entityType = type, entityKey = key))
        } catch (_: Exception) {
            // Roll back on failure.
            _byType.update { current ->
                val existing = current[type].orEmpty()
                current + (type to (existing - key))
            }
        }
    }

    suspend fun remove(type: String, key: String) {
        _byType.update { current ->
            val existing = current[type].orEmpty()
            current + (type to (existing - key))
        }
        try {
            apiHolder.api?.deleteFavorite(type, key)
        } catch (_: Exception) {
            _byType.update { current ->
                val existing = current[type].orEmpty()
                current + (type to (existing + key))
            }
        }
    }
}
