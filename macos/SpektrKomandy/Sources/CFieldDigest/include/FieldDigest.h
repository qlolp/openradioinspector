#pragma once
#include <stdint.h>
#include <stddef.h>
#include <zlib.h>
#ifdef __APPLE__
#include <CommonCrypto/CommonDigest.h>
typedef CC_SHA256_CTX FieldSHA256;
#define FIELD_INIT CC_SHA256_Init
#define FIELD_UPDATE CC_SHA256_Update
#define FIELD_FINAL CC_SHA256_Final
#else
#define OPENSSL_API_COMPAT 0x10101000L
#include <openssl/sha.h>
typedef SHA256_CTX FieldSHA256;
#define FIELD_INIT SHA256_Init
#define FIELD_UPDATE SHA256_Update
#define FIELD_FINAL SHA256_Final
#endif

// The reader supplies at most one bounded record or chunk per update.
static inline void field_digest_init(FieldSHA256 *state) { FIELD_INIT(state); }
static inline void field_digest_update(FieldSHA256 *state, const void *bytes, uint32_t count) {
    FIELD_UPDATE(state, bytes, count);
}
static inline void field_digest_final(FieldSHA256 *state, unsigned char result[32]) {
    FIELD_FINAL(result, state);
}
static inline uint32_t field_crc(uint32_t last, const void *bytes, uint32_t count) {
    return (uint32_t)crc32(last, bytes, count);
}
